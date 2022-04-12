#!/usr/bin/env python3
"""
Tool for creating/updating offline (slob) copies of the 1911 Encyclopædia
Britannica from Wikisource.

Should be used from the command line. Examples:

1. Create slob:

$ ./eb1911.py slob -n -i entries/all.json -o eb1911.slob

2. Fetch all missing pages (normalized)

$ ./eb1911.py fetch -m -n --titles @list2.txt -i data/all.json.bz -o /tmp/all.json -n

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
from datetime import datetime
import os.path

UA = 'EB1911/0.1.0 (dcampos@github/eb1911)'
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

    def __init__(self, in_file=None, out_file=None, progress=True):
        self.site = mwclient.Site('en.wikisource.org', clients_useragent=UA)
        self.in_file = in_file
        self.out_file = out_file
        self.progress = progress

    def output(self, func, count=None, pat='Processing {} of {}'):
        i = 1
        if self.out_file:
            with open(self.out_file, 'w+') as out:
                for entry in func():
                    out.write(entry + '\n')
                    if count and self.progress:
                        show_progress(pat, i, count)
                    i += 1
        else:
            for entry in func():
                print(entry)
                i += 1

    def read_timestamp(self) -> str:
        if self.in_file:
            mtime = os.path.getmtime(self.in_file)
            dt = datetime.fromtimestamp(mtime)
            return dt.strftime('%Y%m%d')
        return ''

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

    def detect_range(self, data):
        soup = BeautifulSoup(data['content'], 'html.parser')
        volume = None
        start = sys.maxsize
        end = 0
        for div in soup.find_all('span', {'class': 'pagenum'}):
            try:
                page_name = div['data-page-name']
                m = re.match(r'Page:EB1911 - Volume (\d+).djvu/\d+', page_name)
                if not volume:
                    volume = int(m.group(1))
                index = int(div['data-page-index'])
            except KeyError:
                break
            start = min(start, index)
            end = max(end, index)
        else:
            return (volume, start, end)
        return (volume, None, None)

    def range_changed(self, data, ranges):
        volume, start, end = data['volume'], data['start'], data['end']
        if volume not in ranges:
            return False
        for i in range(start, end + 1):
            if i in ranges[volume]:
                return True
        return False

    def detect_missing(self, entries):
        found = []
        for line in self.read_input():
            data = json.loads(line)
            found.append(data['page'])
        missing = list(set(entries) - set(found))
        return missing

    def num_entries(self):
        if self.in_file and self.in_file.endswith('.json'):
            num_entries = sum(1 for _ in open(self.in_file))
        else:
            num_entries = None
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
            namespace='0|104',
            toponly=1,
            type="edit|new",
            dir="newer",
            show="!redirect|!anon|!bot",
        )
        result = {}
        ranges = {}
        for change in changes:
            # print(change)
            title = change['title']
            if title.startswith(PREFIX):
                result[title] = change
            elif title.startswith('Page:EB1911'):
                print('Page updated:', title, file=sys.stderr)
                m = re.match(r'Page:EB1911 - Volume (\d+).djvu/(\d+)', title)
                volume = int(m.group(1))
                index = int(m.group(2))
                if volume not in ranges:
                    ranges[volume] = []
                ranges[volume].append(index)
        return result, ranges

    def update(self, start=0, limit=None, timestamp=None, normalize=False):
        if not self.in_file:
            raise Exception('Update requires an input file')
        if not timestamp:
            timestamp = self.read_timestamp()
        timestamp = timestamp.ljust(14, '0')
        changes, ranges = self.list_changes(timestamp)
        num_entries = self.num_entries()
        def result():
            normalizer = Normalizer()
            count = 0
            updated = {}
            for line in self.read_input(start, limit):
                data = json.loads(line)
                title = data['page']
                if 'volume' not in data or 'start' not in data or 'end' not in data:
                    (volume, s, e) = self.detect_range(data)
                    data['volume'] = volume
                    data['start'] = s
                    data['end'] = e
                range_changed = self.range_changed(data, ranges)
                page_changed = False
                if title in changes:
                    change = changes[title]
                    if change['revid'] > data['revid']:
                        page_changed = True
                        data['revid'] = change['revid']
                        data['pageid'] = change['pageid']
                if page_changed or range_changed:
                    count += 1
                    if page_changed:
                        print(f'Article updated: {title}', file=sys.stderr)
                    else:
                        print(f'Range updated: {title}', file=sys.stderr)
                    result = self.fetch_page(title)
                    data['content'] = result['text']['*']
                    (volume, s, e) = self.detect_range(data)
                    data['volume'] = volume
                    data['start'] = s
                    data['end'] = e
                    updated[title] = True
                    if normalize:
                        data = normalizer.normalize(data)
                yield json.dumps(data).decode('utf-8')

            # Fetch missing pages
            for change in changes.values():
                title = change['title']
                if title in updated or title.startswith('Page:'):
                    continue
                count += 1
                if change['type'] == 'new':
                    print(f'New page: {title}', file=sys.stderr)
                else:
                    print(f'Missing page: {title}', file=sys.stderr)
                res = self.fetch_page(title)
                page = {
                    'page': res['title'],
                    'pageid': res['pageid'],
                    'revid': res['revid'],
                    'content': res['text']['*']
                }
                if normalize:
                    page = normalizer.normalize(page)
                yield json.dumps(page).decode('utf-8')

            if count == 0:
                print('Already up-to-date')

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
                (volume, s, e) = self.detect_range(page)
                print(volume)
                page['volume'] = volume
                page['start'] = s
                page['end'] = e
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
            if self.progress:
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

        if not self.out_file:
            raise Exception('No output file defined')

        if os.path.exists(self.out_file):
            print(f'Output file {self.out_file} file already exists!')
            sys.exit(-1)

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
                if self.progress:
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
    parser.add_argument('--titles', '-t', help='List of titles separated by "|", or a file if started with "@"')
    parser.add_argument('--outfile', '-o', help='Output file')
    parser.add_argument('--infile', '-i', help='Input file')
    parser.add_argument('--missing', '-m', action='store_true', help='Fetch missing only')
    parser.add_argument('--normalize', '-n', action='store_true', help='Normalize content (remove comments, etc.)')
    parser.add_argument('--start', '-s', default=0, type=int, help='Start from this index')
    parser.add_argument('--limit', '-l', default=sys.maxsize, type=int, help='Process these many entries')
    parser.add_argument('--timestamp', '-T', type=str, help='Timstamp')
    parser.add_argument('--goldendict', '-g', action='store_true', help='Optimize for goldendict')
    parser.add_argument('--no-progress', '-N', action='store_false', help='Don\'t show progress')
    args = parser.parse_args()

    fetcher = Fetcher(out_file=args.outfile, in_file=args.infile, progress=args.no_progress)

    if args.cmd == 'list':
        fetcher.list_pages()
    elif args.cmd == 'fetch':
        fetcher.fetch(args.titles, missing=args.missing, normalize=args.normalize)
    elif args.cmd == 'update':
        # print('timestamp:', args.timestamp)
        fetcher.update(limit=args.limit, start=args.start, timestamp=args.timestamp, normalize=args.normalize)
    elif args.cmd == 'slob':
        fetcher.write_slob(goldendict=args.goldendict)
    elif args.cmd == 'normalize':
        fetcher.normalize()
    # elif args.latest:
    #     fetcher = Fetcher()
    #     fetcher.get_latest_revision(args.titles)
