#!/usr/bin/env python3
"""
Build winter pool documents

Usage:
    tool.py (-h | --help)
    tool.py [--quiet] [--auth-bind=ADDRESS] [--auth-hostname=HOSTNAME]
        [--auth-port=PORT] [--loop] [--loop-sleep=NUMBER] [--spec=FILE]

Options:

    -h, --help                  Show a brief usage summary.
    --quiet                     Decrease logging verbosity.

    --spec=FILE                 Job specification file.
                                [default: ./jobspec.yaml]

    --loop                      Keep running the pipeline. If not specified,
                                the script exits after the first run.
    --loop-sleep=NUMBER         If looping, number of seconds to sleep between
                                iterations [default: 600]

    --auth-bind=ADDRESS         Host/ip to bind web server to.
                                [default: localhost]
    --auth-hostname=HOSTNAME    Hostname to use when running a local web
                                server. [default: localhost]
    --auth-port=PORT            Port to bind local web server to.
                                [default: 8080]

"""
import csv
import logging
import os
import random
import re
import secrets
import sys
import tempfile
import time

import docopt
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import httplib2shim
import jinja2.loaders
import latex
import latex.jinja2
from oauth2client import file, client, tools
import textract
import yaml

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.appfolder',
    'https://www.googleapis.com/auth/drive.readonly',
]

PAGE_SIZE = 200

UCAS_PERSONAL_ID_PATTERN = re.compile(r'UCAS Personal ID:? ([0-9]+)')

# For persnal id match lines, this regex tries to match the name
NAME_PATTERN = re.compile(r'([^\s].*)\s+[0-9]+\sUCAS Personal ID')

LOG = logging.getLogger(__name__)


def main():
    # Seed the random number generator with OS randomness
    random.seed(os.urandom(32), version=2)

    opts = docopt.docopt(__doc__)
    with open(opts['--spec']) as fobj:
        spec = yaml.load(fobj)

    logging.basicConfig(
        level=logging.WARN if opts['--quiet'] else logging.INFO
    )

    store_dir = spec.get(
        'store_path', os.path.join(os.path.dirname(__file__), 'store'))
    if not os.path.exists(store_dir):
        os.makedirs(store_dir)

    LOG.info('Using %s for token storage directory.', store_dir)
    store = file.Storage(os.path.join(store_dir, 'token.json'))
    creds = store.get()

    http = httplib2shim.Http()

    client_secrets_path = spec.get(
        'client_secrets_path', './client_secrets.json')
    LOG.info('Using %s for client secrets', client_secrets_path)
    if not creds or creds.invalid:
        flow = client.flow_from_clientsecrets(client_secrets_path, SCOPES)
        creds = run_flow(flow, store, opts=opts, http=http)
    service = build('drive', 'v3', http=creds.authorize(http))

    loop_sleep = int(opts['--loop-sleep'])
    while True:
        run_pipeline(service, spec)
        if not opts['--loop']:
            break
        LOG.info('Sleeping for %s seconds', loop_sleep)
        time.sleep(loop_sleep)


def run_pipeline(service, spec):
    incoming_folder_id = spec['incoming_folder_id']
    processed_folder_id = spec['processed_folder_id']

    def fetch_incoming_files():
        return fetch_incoming_files_from_folder(service, incoming_folder_id)

    def fetch_processed_files():
        return fetch_processed_files_from_folder(service, processed_folder_id)

    while True:
        did_things = []

        LOG.info('Copying new incoming files')
        did_things.append(copy_new_incoming_files(
            service, fetch_incoming_files(), fetch_processed_files(),
            processed_folder_id))

        LOG.info('OCR-ing any non-OCR-ed files')
        did_things.append(ocr_files(
            service, fetch_processed_files(), processed_folder_id))

        LOG.info('Extracting applicant info from OCR-ed text')
        did_things.append(extract_ucas_personal_id(
            service, fetch_processed_files()))

        LOG.info('Did things: %r', did_things)

        if any(did_things):
            LOG.info('Generating index and summary documents')
            processed = fetch_processed_files()
            generate_index(service, processed, processed_folder_id)
            generate_summary(service, processed, processed_folder_id)

        # We're done as soon as we had nothing to do
        if not any(did_things):
            break


def list_all_files(service, shuffled=True, **kwargs):
    files = []
    pageToken = None

    while True:
        results = service.files().list(pageToken=pageToken, **kwargs).execute()
        files.extend(results.get('files', []))

        pageToken = results.get('nextPageToken', '')
        if pageToken == '':
            break

    if shuffled:
        random.shuffle(files)

    return files


def fetch_incoming_files_from_folder(service, incoming_folder_id):
    return list_all_files(
        service,
        pageSize=PAGE_SIZE,
        includeTeamDriveItems=True,
        supportsTeamDrives=True,
        q=(
            f"'{incoming_folder_id}' in parents"
            " and mimeType = 'application/pdf'"
            " and trashed = false"
        ),
        fields="nextPageToken, files(id, name, mimeType)"
    )


def fetch_processed_files_from_folder(service, processed_folder_id):
    return list_all_files(
        service,
        pageSize=PAGE_SIZE,
        includeTeamDriveItems=True,
        supportsTeamDrives=True,
        q=f"'{processed_folder_id}' in parents and trashed = false",
        fields=(
            "nextPageToken, "
            "files(id, name, appProperties, mimeType, webViewLink)"
        )
    )


def file_has_properties(file, keys):
    appProperties = file.get('appProperties', {})
    return all(k in appProperties for k in keys)


def copy_new_incoming_files(service, incoming, processed, processed_folder_id):
    processed_file_sources = [
        cf for cf in (
            item.get('appProperties', {}).get('copiedFrom')
            for item in processed
        ) if cf is not None
    ]

    for item in incoming:
        if item['id'] in processed_file_sources:
            continue

        name = secrets.token_urlsafe() + '.pdf'
        LOG.info('Copying %s to %s', item['name'], name)
        service.files().copy(
            fileId=item['id'],
            supportsTeamDrives=True,
            body={
                'name': name,
                'copyRequiresWriterPermission': True,
                'appProperties': {
                    'copiedFrom': item['id'],
                },
                'parents': [processed_folder_id],
            },
        ).execute()

        # After a successful processing of a file, return to let other
        # pipeline stages progress
        return True

    return False


def ocr_files(service, processed, processed_folder_id):
    for item in processed:
        appProperties = item.get('appProperties', {})

        copiedFrom = appProperties.get('copiedFrom')
        if copiedFrom is None or item['mimeType'] != 'application/pdf':
            continue

        if file_has_properties(item, ['ocrTextFileId']):
            continue

        basename = '.'.join(item['name'].split('.')[:-1])

        LOG.info('Downloading and OCR-ing %s', item["name"])
        with tempfile.TemporaryDirectory() as tmp_dir:
            request = service.files().get_media(
                fileId=item['id'], supportsTeamDrives=True)

            download_path = os.path.join(tmp_dir, 'file.pdf')
            with open(download_path, 'wb') as fobj:
                downloader = MediaIoBaseDownload(fobj, request)
                while True:
                    _, done = downloader.next_chunk()
                    if done:
                        break

            text_path = os.path.join(tmp_dir, 'file.txt')
            text = textract.process(download_path, method='tesseract')
            with open(text_path, 'w') as fobj:
                fobj.write(text.decode('utf8', errors='replace'))

            LOG.info("Uploading text")
            media = MediaFileUpload(text_path, mimetype='text/plain')
            text_file = service.files().create(
                body={
                    'name': basename + '.txt',
                    'copyRequiresWriterPermission': True,
                    'appProperties': {
                        'pdfSourceFileId': item['id'],
                    },
                    'parents': [processed_folder_id],
                },
                supportsTeamDrives=True,
                media_body=media,
                fields='id'
            ).execute()

            service.files().update(
                fileId=item['id'],
                supportsTeamDrives=True,
                body={
                    'appProperties': {
                        'ocrTextFileId': text_file.get('id'),
                    },
                }
            ).execute()

        # After a successful processing of a file, return to let other pipeline
        # stages progress
        return True

    return False


def extract_ucas_personal_id(service, processed):
    processed_by_id = {item['id']: item for item in processed}

    # Grep text values
    for pdf_item in processed:
        pdf_appProperties = pdf_item.get('appProperties', {})
        ocrTextFileId = pdf_appProperties.get('ocrTextFileId')
        if ocrTextFileId is None or pdf_item['mimeType'] != 'application/pdf':
            continue

        required_properties = [
            'ucasPersonalId', 'totalMatchCount', 'consistentMatchCount',
            'extractedName',
        ]
        if file_has_properties(pdf_item, required_properties):
            continue

        item = processed_by_id.get(ocrTextFileId)
        if item is None:
            LOG.warn('Could not find OCR-ed text id: %s', ocrTextFileId)
            continue

        LOG.info('Scanning text of %s', item['id'])
        with tempfile.TemporaryDirectory() as tmp_dir:
            request = service.files().get_media(
                fileId=item['id'], supportsTeamDrives=True)

            download_path = os.path.join(tmp_dir, 'file.pdf')
            with open(download_path, 'wb') as fobj:
                downloader = MediaIoBaseDownload(fobj, request)
                while True:
                    _, done = downloader.next_chunk()
                    if done:
                        break

            with open(download_path) as fobj:
                matched_lines_and_personal_ids = [
                    (line, m.group(1))
                    for line, m in (
                        (line, UCAS_PERSONAL_ID_PATTERN.search(line))
                        for line in fobj.readlines()
                    )
                    if m
                ]

                # Match names
                matched_names = [
                    m.group(1)
                    for m in (
                        NAME_PATTERN.search(line)
                        for line, _ in matched_lines_and_personal_ids
                    )
                    if m
                ]

                name_table = {}
                for name in matched_names:
                    name_table[name] = 1 + name_table.get(name, 0)

                names_by_count = sorted(
                    list(name_table.items()),
                    key=lambda v: -v[1]
                )

                best_name = (
                    names_by_count[0][0]
                    if len(names_by_count) > 0
                    else 'Unknown'
                )

                count_table = {}
                for _, id in matched_lines_and_personal_ids:
                    count_table[id] = 1 + count_table.get(id, 0)

                matches_by_count = sorted(
                    list(count_table.items()),
                    key=lambda v: -v[1]
                )

                if len(matches_by_count) == 0:
                    LOG.warn('No UCAS id matches!')
                    continue

                if matches_by_count[0][1] < 3:
                    LOG.warn(
                        'Too few consistent id matches (%s)',
                        matches_by_count[0][1])
                    continue

                id = matches_by_count[0][0]

                service.files().update(
                    fileId=pdf_item['id'],
                    supportsTeamDrives=True,
                    body={
                        'appProperties': {
                            'ucasPersonalId': id,
                            'consistentMatchCount': matches_by_count[0][1],
                            'totalMatchCount': len(
                                matched_lines_and_personal_ids),
                            'extractedName': best_name,
                        },
                    }
                ).execute()

        # After a successful processing of a file, return to let other pipeline
        # stages progress
        return True

    return False


def generate_index(service, processed, processed_folder_id):
    required_properties = ['ucasPersonalId', 'extractedName']
    fully_processed_files = [
        item for item in processed
        if file_has_properties(item, required_properties)
    ]

    # Sort files
    fully_processed_files = sorted(
        fully_processed_files,
        key=lambda f: (
            f.get('appProperties', {}).get('extractedName', '')
            .split(' ')[-1]
        )
    )

    # Don't try to generate report if no files
    if len(fully_processed_files) == 0:
        return

    # Do we have an index already?
    index_files = [
        item for item in processed
        if item.get('appProperties', {}).get('isIndex', False)
    ]

    # Make PDF
    env = latex.jinja2.make_env(loader=jinja2.loaders.FileSystemLoader(
        os.path.join(os.path.dirname(__file__), 'templates')
    ))
    template = env.get_template('report.template.tex')
    pdf = latex.build_pdf(template.render(files=fully_processed_files))

    with tempfile.TemporaryDirectory() as tmpdir:
        outpath = os.path.join(tmpdir, 'index.pdf')
        with open(outpath, 'wb') as fobj:
            fobj.write(bytes(pdf))

        # Upload PDF
        media = MediaFileUpload(outpath, mimetype='application/pdf')
        api_params = {
            'body': {
                'name': 'index.pdf',
                'copyRequiresWriterPermission': True,
                'appProperties': {
                    'isIndex': True,
                },
            },
            'supportsTeamDrives': True,
            'media_body': media,
            'fields': 'id'
        }

        if len(index_files) == 0:
            api_params['body']['parents'] = [processed_folder_id]
            service.files().create(**api_params).execute()
        else:
            for item in index_files:
                service.files().update(
                    fileId=item['id'], **api_params).execute()


def generate_summary(service, processed, processed_folder_id):
    processed_by_id = {item['id']: item for item in processed}

    required_properties = ['ucasPersonalId', 'extractedName']
    fully_processed_files = [
        item for item in processed
        if file_has_properties(item, required_properties)
    ]

    # Sort files
    fully_processed_files = sorted(
        fully_processed_files,
        key=lambda f: (
            f.get('appProperties', {}).get('extractedName', '')
            .split(' ')[-1]
        )
    )

    # Don't try to generate summary if no files
    if len(fully_processed_files) == 0:
        return

    # Do we have an summary already?
    summary_files = [
        item for item in processed
        if item.get('appProperties', {}).get('isSummary', False)
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Make CSV
        outpath = os.path.join(tmpdir, 'summary.csv')
        with open(outpath, 'w') as fobj:
            w = csv.writer(fobj)
            w.writerow([
                'UCAS Personal ID', 'Extracted Name', 'PDF', 'Extracted text'
            ])
            for item in fully_processed_files:
                appProperties = item.get('appProperties', {})
                text_item = processed_by_id.get(
                    appProperties.get('ocrTextFileId'))
                w.writerow([
                    appProperties['ucasPersonalId'],
                    appProperties['extractedName'],
                    item['webViewLink'],
                    text_item['webViewLink'] if text_item is not None else ''
                ])

        # Upload - allow downloads otherwise this is a little pointless
        media = MediaFileUpload(outpath, mimetype='text/csv')
        api_params = {
            'body': {
                'name': 'summary.csv',
                'appProperties': {
                    'isSummary': True,
                },
            },
            'supportsTeamDrives': True,
            'media_body': media,
            'fields': 'id'
        }

        if len(summary_files) == 0:
            api_params['body']['parents'] = [processed_folder_id]
            service.files().create(**api_params).execute()
        else:
            for item in summary_files:
                service.files().update(
                    fileId=item['id'], **api_params).execute()


def run_flow(flow, storage, opts, http=None):
    """Based on
    https://oauth2client.readthedocs.io/en/latest/source/oauth2client.tools.html#oauth2client.tools.run_flow
    """  # noqa: E501
    bind, port = opts['--auth-bind'], int(opts['--auth-port'])
    LOG.info('Starting authorisation server on %s:%s', bind, port)
    server = tools.ClientRedirectServer(
        (bind, port), tools.ClientRedirectHandler)

    hostname = opts['--auth-hostname']
    flow.redirect_uri = f'http://{hostname}:{port}'

    authorize_url = flow.step1_get_authorize_url()
    LOG.info('Open the following link in a browser:')
    LOG.info('%s', authorize_url)

    server.handle_request()
    if 'error' in server.query_params:
        sys.exit('Authentication request was rejected.')
    if 'code' in server.query_params:
        code = server.query_params['code']
    else:
        sys.exit('"code" not in query parameters of redirect')

    try:
        credential = flow.step2_exchange(code, http=http)
    except client.FlowExchangeError as e:
        sys.exit(f'Authentication has failed: {e}')

    storage.put(credential)
    credential.set_store(storage)

    return credential


if __name__ == '__main__':
    main()
