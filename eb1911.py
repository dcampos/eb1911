#!/usr/bin/env python3
"""
Tool for creating/updating offline (slob) copies of the 1911 Encyclopædia
Britannica from Wikisource.

Should be used from the command line. Examples:

1. Create slob:

$ ./eb1911.py slob -n -i entries/all.json -o eb1911.slob

2. Fetch all missing pages (normalized)

$ ./eb1911.py fetch -m -n --titles @list2.txt -o /tmp/all.json -n

3. Fetch a list of pages:

$ ./eb1911.py list -o entrylist.txt

4. Normalize content:

$ ./eb1911.py normalize -i entries/all.json -o entries/normalized.json
"""

import os
import re
import sys
from typing import Dict, List
from bs4 import BeautifulSoup
from bs4.element import Comment
import mwclient
import argparse
import orjson as json
import slob
import itertools


PREFIX = '1911 Encyclopædia Britannica'
HTML_TEXT = 'text/html; charset=utf-8'
TITLE = '1911 Encyclopædia Britannica'
LICENSE_NAME = 'Creative Commons Attribution-Share Alike 3.0'
LICENSE_URL = 'https://creativecommons.org/licenses/by-sa/3.0/'
SOURCE = 'https://en.wikisource.org/wiki/1911_Encyclop%C3%A6dia_Britannica'
URI = 'https://en.wikisource.org/wiki/1911_Encyclop%C3%A6dia_Britannica'
DESCRIPTION = TITLE
CSS_LINKS = (
    '<link rel="stylesheet" href="~/css/site.styles.css" type="text/css">'
    '<link rel="stylesheet" href="~/css/ext.gadget.css" type="text/css">'
    '<link rel="stylesheet" href="~/css/custom.css" type="text/css">'
)

def show_progress(pat, i, num_entries=None):
    percent = i / num_entries
    fill = '=' * int(percent * 20)
    if i < num_entries and fill:
        fill = fill[:-1] + '>'
    bar = ' [' + fill + ('·' *  (20 - len(fill))) + '] {}%'.format(round(percent * 100))
    print(pat.format(i, num_entries) + bar, end='\r', file=sys.stderr)

def observer(event):
    if event[0] == 'begin_sort':
        print(f'Sorting...')
    elif event[0] == 'begin_resolve_aliases':
        print(f'Resolving aliases...')
    elif event[0] == 'begin_move':
        print(f'Moving {event[1]}...')


class Fetcher:

    def __init__(self, in_file=None, out_file=None):
        self.site = mwclient.Site('en.wikisource.org', clients_useragent='EB1911/0.1')
        self.in_file = in_file
        self.out_file = out_file

    def output(self, func, count=None, pat='Processing {} of {}'):
        i = 1
        if self.out_file:
            with open(self.out_file, 'w+') as out:
                for entry in func():
                    out.write(entry + '\n')
                    if count:
                        show_progress(pat, i, count)
                    i += 1
        else:
            for entry in func():
                print(entry)
                i += 1

    def read_input(self, start=0, limit=sys.maxsize):
        if self.in_file:

            if self.in_file.endswith('.json'):
                file = open(self.in_file, 'r+')
            elif self.in_file.endswith('.gz'):
                import gzip
                file = gzip.open(self.in_file, 'r')
            else:
                import bz2
                file = bz2.BZ2File(self.in_file, 'r')

            with file as lines:
                for line in itertools.islice(lines, start, start + limit):
                    yield line

    def detect_missing(self, entries):
        found = []
        for line in self.read_input():
            data = json.loads(line)
            found.append(data['page'])
        missing = list(set(entries) - set(found))
        return missing

    def num_entries(self):
        num_entries = sum(1 for _ in open(self.in_file))
        return num_entries

    def list_pages(self, prefix=PREFIX):
        allpages = self.site.allpages(prefix=prefix)
        def result():
            for page in allpages:
                yield page.page_title
        self.output(result)

    def fetch_page(self, title):
        result = self.site.parse(page=title)
        # print(json.dumps(result).decode('utf-8'))
        return result

    def get_latest_revision(self, title):
        result = self.site.api('query', prop='revisions', titles=title, rvlimit=1, rvprop='ids')
        pages = result["query"]["pages"]
        page = list(pages.values())[0]
        return {
            'pageid': page['pageid'],
            'title': page['title'],
            'revid': page['revisions'][0]['revid']
        }

    def list_changes(self, timestamp):
        changes = self.site.recentchanges(
            start=timestamp,
            namespace=0,
            toponly=1,
            type="edit|new",
            dir="newer",
            show="!redirect|!anon|!bot",
        )
        result = {}
        for change in changes:
            # print(change)
            title = change['title']
            if title.startswith(PREFIX):
                result[title] = change
        return result

    def update(self, start=0, limit=None, timestamp=None, normalize=False):
        if not self.in_file:
            raise Exception('Update requires an input file')
        changes = self.list_changes(timestamp)
        num_entries = self.num_entries()
        def result():
            normalizer = Normalizer()
            for line in self.read_input(start, limit):
                data = json.loads(line)
                # print(data)
                title = data['page']
                # print(title)
                # revdata = self.get_latest_revision(title)
                # print(revdata, file=sys.stderr)
                if title in changes:
                    change = changes[title]
                    if change['revid'] > data['revid']:
                        print(f'==> Page updated: {title}', file=sys.stderr)
                        result = self.fetch_page(title)
                        data['content'] = result['text']['*']
                        data['revid'] = change['revid']
                        data['pageid'] = change['pageid']
                        if normalize:
                            data = normalizer.normalize(data)
                yield json.dumps(data).decode('utf-8')
        self.output(result, num_entries)

    def fetch(self, titles, missing=False, normalize=True):
        if not titles:
            raise Exception('Fetch requires a list of titles')

        entries = []

        if titles.startswith('@'):
            # Read file containing titles
            fname = titles[1:]
            with open(fname, 'r+') as lines:
                for line in lines:
                    entries.append(line[:-1])
        else:
            entries = titles.split('|')

        if missing:
            if not self.in_file:
                raise Exception('Fetch missing requires an input file')
            entries = self.detect_missing(entries)
            print(f'missing entries: {entries}', file=sys.stderr)

        normalizer = Normalizer()

        def result():
            for entry in entries:
                res = self.fetch_page(entry)
                page = {
                    'page': res['title'],
                    'pageid': res['pageid'],
                    'revid': res['revid'],
                    'content': res['text']['*']
                }
                if normalize:
                    page = normalizer.normalize(page)
                yield json.dumps(page).decode('utf-8')
        self.output(result, len(entries), 'Fetching {} of {}')

    def prepare_entries(self):
        dictionary = []
        seen = {}
        count = 0
        ignored = 0
        duplicated = 0
        for line in self.read_input():
            count += 1
            print(f'Reading entry {count}', end='\r')
            headwords = []
            data = json.loads(line)
            html = data['content']
            title = data['page'] #.split('/', 1)[1]
            # Remove prefix
            if title.startswith(PREFIX + '/'):
                title = title.split('/', 1)[1]
            else:
                # Ignore wrongly named pages
                print(f'*** Ignoring page {title}')
                ignored += 1
                continue
            if title in seen:
                print(f'*** Duplicate entry: {title}')
                duplicated += 1
                continue
            # if args.aard2:
            # html = re.sub('"gdlookup://localhost/(.*?)"', replace_link(page), html)
            # print(page)
            seen[title] = True
            headwords.append(title)
            dictionary.append((headwords, html))

        print()
        print(f'Total:      {count}')
        print(f'Ignored:    {ignored}')
        print(f'Duplicated: {duplicated}')
        return dictionary

    def write_slob(self, goldendict=False):
        dictionary = self.prepare_entries()
        with slob.create(self.out_file, min_bin_size=512*1024, observer=observer) as s:
            num_entries = len(dictionary)
            if not goldendict:
                print('Note: goldendict compatibility not set')
            for i, entry in enumerate(dictionary, 1):
                # percent = round(i / num_entries * 100)
                # print(f"Adding {i} of {num_entries} ({percent}%)", end='\r')
                content = entry[1]
                if goldendict:
                    content = re.sub('href=\"((?!(http|/|#)).*?)\"', r'href="gdlookup://localhost/\1"', content)
                content = CSS_LINKS + content
                show_progress("Adding {} of {}", i, num_entries)
                s.add(content.encode('utf-8'), *entry[0], content_type=HTML_TEXT)
            include_types = {"js", "css", "images"}
            include_path = os.path.join(os.path.dirname(__file__), 'include')
            slob.add_dir(s, include_path, prefix='~/', include_only=include_types)
            # with open('playsound.png', 'rb') as png:
            #     data = png.read()
            #     s.add(data, 'playsound.png', 'image/png')
            s.tag('label', TITLE)
            s.tag('license.url', LICENSE_URL)
            s.tag('license.name', LICENSE_NAME)
            s.tag('source', SOURCE)
            s.tag('uri', URI)
            print('\n==> Finishing slob...')

    def normalize(self):
        if not self.in_file:
            raise Exception('Normalize requires an input file')
        normalizer = Normalizer()
        num_entries = self.num_entries()
        def result():
            for line in self.read_input():
                data = json.loads(line)
                # print(data)
                data = normalizer.normalize(data)
                yield json.dumps(data).decode('utf-8')
        self.output(result, num_entries)


class Normalizer:

    def __init__(self):
        pass

    def normalize_ref(self, value):
        article = value.group(1)
        article = article.replace('_', '%20')
        article = article.replace('/', '%2F')
        return f'href=\"{article}\"'

    def fix_links(self, content):
        # Internal references
        content = re.sub('href=\"/wiki/1911_Encyclop%C3%A6dia_Britannica/(.*?)\"', self.normalize_ref, content)
        # Other pages
        content = re.sub('href=\"/wiki/', 'href=\"https://en.wikisource.org/wiki/', content)
        # /w/ special image source
        content = re.sub('src=\"/w/', 'src=\"https://en.wikisource.org/w/', content)
        # Wikisource links, etc.
        content = re.sub('href=\"((?!(http|/|#)).*?)\"', self.normalize_ref, content)
        return content

    def fix_imgs(self, content):
        content = re.sub('src=\"//', 'src=\"https://', content)
        return content

    def clean_html(self, content):
        soup = BeautifulSoup(content, 'html.parser')
        # Delete header
        for div in soup.find_all('div', {'id': 'headerContainer'}):
            div.decompose()
        # Delete edit links
        for span in soup.find_all('span', {'class': 'mw-editsection'}):
            span.decompose()
        # Delete comments
        main = soup.find('div', {'class': 'mw-parser-output'})
        for element in main(text=lambda text: isinstance(text, Comment)):
            element.extract()
        return str(soup)

    def normalize(self, page):
        content = page['content']
        content = self.fix_links(content)
        content = self.fix_imgs(content)
        content = self.clean_html(content)
        page['content'] = content
        return page


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('cmd', help='Command to run', choices=['list', 'fetch', 'update', 'slob', 'normalize'])
    # parser.add_argument('fetch', help='Fetch articles')
    # parser.add_argument('--latest', action='store_true', help='Fetch latest revision')
    # parser.add_argument('update', help='Update pages')
    parser.add_argument('--titles', help='List of titles separated by "|", or a file if started with "@"')
    parser.add_argument('--outfile', '-o', help='Output file')
    parser.add_argument('--infile', '-i', help='Input file')
    parser.add_argument('--missing', '-m', action='store_true', help='Fetch missing only')
    parser.add_argument('--normalize', '-n', action='store_true', help='Normalize content (remove comments, etc.)')
    parser.add_argument('--start', '-s', default=0, type=int, help='Start from this index')
    parser.add_argument('--limit', '-l', default=sys.maxsize, type=int, help='Process these many entries')
    parser.add_argument('--timestamp', '-t', default=sys.maxsize, type=str, help='Timstamp')
    parser.add_argument('--goldendict', '-g', action='store_true', help='Optimize for goldendict')
    args = parser.parse_args()

    if args.cmd == 'list':
        fetcher = Fetcher(out_file=args.outfile, in_file=args.infile)
        fetcher.list_pages()
    elif args.cmd == 'fetch':
        fetcher = Fetcher(out_file=args.outfile, in_file=args.infile)
        fetcher.fetch(args.titles, missing=args.missing, normalize=args.normalize)
    elif args.cmd == 'update':
        fetcher = Fetcher(out_file=args.outfile, in_file=args.infile)
        fetcher.update(limit=args.limit, start=args.start, timestamp=args.timestamp, normalize=args.normalize)
    elif args.cmd == 'slob':
        fetcher = Fetcher(out_file=args.outfile, in_file=args.infile)
        fetcher.write_slob(goldendict=args.goldendict)
    elif args.cmd == 'normalize':
        fetcher = Fetcher(out_file=args.outfile, in_file=args.infile)
        fetcher.normalize()
    # elif args.latest:
    #     fetcher = Fetcher()
    #     fetcher.get_latest_revision(args.titles)
