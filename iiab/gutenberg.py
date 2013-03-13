# -*- coding: utf-8 -*-

import re

from flask import (Blueprint, render_template, current_app, request, Response,
                   flash, url_for, redirect, session, abort, safe_join,
                   send_file)
from flaskext.babel import gettext as _
import json

import whoosh
from whoosh.index import open_dir
from whoosh.qparser import MultifieldParser
from .whoosh_multi_field_spelling_correction import MultiFieldQueryCorrector

from .extensions import db
from gutenberg_models import (GutenbergBook, GutenbergBookFile, 
        GutenbergCreator, gutenberg_books_creator_map)

import pagination_helper
from .endpoint_description import EndPointDescription

gutenberg = Blueprint('gutenberg', __name__, url_prefix='/books')
etext_regex = re.compile(r'^etext(\d+)$')
DEFAULT_SEARCH_COLUMNS = ['title', 'creator', 'contributor'] # names correspond to fields in whoosh schema
DEFAULT_RESULTS_PER_PAGE = 20

@gutenberg.route('/')
def index():
    return render_template('gutenberg/index.html')


@gutenberg.route('/search')
def search():
    query = request.args.get('q', '').strip()
    pagination = None
    if query:
        page = int(request.args.get('page', 1))
        (pagination, suggestion) = paginated_search(query, page)
    else:
        flash(_('Please input keyword(s)'), 'error')
    print pagination.items
    return render_template('gutenberg/search.html', pagination=pagination, keywords=query, suggestion=suggestion, endpoint_desc=EndPointDescription('gutenberg.search', None))

def paginated_search(query_text, page=1, pagelen=DEFAULT_RESULTS_PER_PAGE):
    """
    Return a tuple consisting of an object that emulates an SQLAlchemy pagination object and corrected query suggestion
    pagelen specifies number of hits per page
    page specifies page of results (first page is 1)
    """
    index_dir = current_app.config['GUTENBERG_INDEX_DIR']
    query_text = unicode(query_text)  # Must be unicode
    ix = open_dir(index_dir)
    sort_column = 'creator'
    with ix.searcher() as searcher:
        query = MultifieldParser(DEFAULT_SEARCH_COLUMNS, ix.schema).parse(query_text)
        try:
            # search_page returns whoosh.searching.ResultsPage
            results = searcher.search_page(query, page, pagelen=pagelen, sortedby=sort_column)
            total = results.total
        except ValueError, e:  # Invalid page number
            results = []
            total = 0
        paginate = pagination_helper.Pagination(page, pagelen, total, [dict(r.items()) for r in results])
        corrections = deduplicate_corrections(get_query_corrections(searcher, query, query_text))  # list of Corrector objects

        #hf = whoosh.highlight.HtmlFormatter(classname="change")
        #html = corrections.format_string(hf)
        return (paginate, [c.string for c in corrections])


def deduplicate_corrections(corrections):
    """
    Return list of correction that omits entries where the query is unmodified
    :param corrections: list of Corrector objects
    :returns: list of Corrector objects
    """
    # Using values from a dictionary comprehension rather than a list comprehension in order to deduplicate
    return {c.string : c for c in corrections if c.original_query != c.query}.values()

def get_query_corrections(searcher, query, qstring):
    """
    Suggest alternate spelling for search terms by searching each column with
    spelling correction support in turn.

    :param searcher: whoosh searcher object
    :param query: whoosh query object
    :param qstring: search string that was passed to the query object
    :returns: MultiFieldQueryCorrector with one corrector for each corrected column
    """
    fieldnames = [name for name, field in searcher.schema.items() if field.spelling]
    correctors = {}
    for fieldname in fieldnames:
        if fieldname not in correctors:
            correctors[fieldname] = searcher.corrector(fieldname)
    terms = []
    for token in query.all_tokens():
        if token.fieldname in correctors:
            terms.append((token.fieldname, token.text))

    return MultiFieldQueryCorrector(correctors, terms).correct_query(query, qstring)

@gutenberg.route('/titles')
def by_title():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', DEFAULT_RESULTS_PER_PAGE))
    pagination = GutenbergBook.query.order_by(GutenbergBook.title).paginate(page, per_page)
    return render_template('gutenberg/title-index.html', pagination=pagination, endpoint_desc=EndPointDescription('.by_title', dict(per_page=per_page)))

@gutenberg.route('/authors')
def by_author():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', DEFAULT_RESULTS_PER_PAGE))
    pagination = GutenbergCreator.query.order_by(GutenbergCreator.creator).paginate(page, per_page)

    return render_template('gutenberg/author-index.html', pagination=pagination, endpoint_desc=EndPointDescription('.by_author', dict(per_page=per_page)))

@gutenberg.route('/author/<authorId>')
def author(authorId):
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', DEFAULT_RESULTS_PER_PAGE))
    pagination = GutenbergBook.query.filter(gutenberg_books_creator_map.c.creator_id == authorId).filter(gutenberg_books_creator_map.c.book_id == GutenbergBook.textId).paginate(page, per_page)
    return render_template('gutenberg/title-index.html', pagination=pagination, endpoint_desc=EndPointDescription('.author', dict(authorId=authorId, per_page=per_page)))

@gutenberg.route('/text/<textId>/details')
def text(textId):
    print textId
    record = GutenbergBook.query.filter_by(textId=textId).first()
    fields = [
        (_('Title'), 'title', ''),
        (_('Author'), 'gutenberg_creators', 'creator'),
        (_('Contributor'), 'gutenberg_contributors', 'contributor'),
        (_('Subject'), 'gutenberg_subjects', 'subject'),
        (_('Category'), 'gutenberg_categories', 'category'),
        (_('Language'), 'gutenberg_languages', 'language')
        ]
    files = GutenbergBookFile.query.filter_by(textId=textId).all()
    print record
    print fields
    print files
    return render_template('gutenberg/book_details.html', record=record, fields=fields, files=files)

@gutenberg.route('/text/<textId>/<int:textIndex>')
def read(textId, textIndex):
    data_dir = current_app.config['GUTENBERG_ROOT_DIR']
    files = GutenbergBookFile.query.filter_by(textId=textId).all()
    assert textIndex >= 0 and textIndex < len(files)
    fullpath = safe_join(data_dir, files[textIndex].file)
    return send_file(fullpath)

def choose_file(textId):
    files = GutenbergBookFile.query.filter_by(textId=textId)
    #for f in files:
    #    print f
    return files[0].file

@gutenberg.route('/autocomplete')
def autocomplete():
    term = request.args.get('term', '')
    if term != '':
        index_dir = current_app.config['GUTENBERG_INDEX_DIR']
        ix = open_dir(index_dir)
        with ix.searcher() as searcher:
            # might use whoosh.analysis.*Analyzer to break query up
            # for matching. However it isn't clear how to combine completion
            # of partial matches across several different columns without
            # lots of effort

            # Need to reimplement this...
            # corrector only matches with hamming distance of 2 by default. 
            # Besides, autocomplete should not be a spelling correction but
            # rather partial matches.
            corrections = []
            for col in DEFAULT_SEARCH_COLUMNS:
                corrections += searcher.corrector(col).suggest(term)
            return Response(response=json.dumps(corrections), mimetype="application/json")
    else:
        # Choosing an inefficient redirect because still testing different
        # approaches and its easier to centralize the handling.  If we keep
        # this approach we can just change the referencing url
        return redirect(url_for("static", filename="gutenberg_wordlist.json"))
