from __future__ import print_function
import secrets

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from httplib2 import Http
from oauth2client import file, client, tools
import yaml
import tempfile
import os
import textract
import re

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.appfolder',
    'https://www.googleapis.com/auth/drive.readonly',
]

PAGE_SIZE = 20

UCAS_PERSONAL_ID_PATTERN = re.compile(r'UCAS Personal ID:? ([0-9]+)')


def main():
    """Shows basic usage of the Drive v3 API.
    Prints the names and ids of the first 10 files the user has access to.
    """
    with open('jobspec.yaml') as fobj:
        spec = yaml.load(fobj)

    incoming_folder_id = spec['incoming_folder_id']
    processed_folder_id = spec['processed_folder_id']

    store = file.Storage('token.json')
    creds = store.get()
    if not creds or creds.invalid:
        flow = client.flow_from_clientsecrets('credentials.json', SCOPES)
        creds = tools.run_flow(flow, store)
    service = build('drive', 'v3', http=creds.authorize(Http()))

    # Get list of incoming PDF files
    incoming = list_all_files(
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
    # print(len(incoming))

    # Get list of processed files
    processed = list_all_files(
        service,
        pageSize=PAGE_SIZE,
        includeTeamDriveItems=True,
        supportsTeamDrives=True,
        q=f"'{processed_folder_id}' in parents and trashed = false",
        fields="nextPageToken, files(id, name, appProperties, mimeType)"
    )

    for item in processed:
        print(repr(item))

    # print(len(processed))

    processed_file_sources = [
        cf for cf in (
            item.get('appProperties', {}).get('copiedFrom')
            for item in processed
        ) if cf is not None
    ]

    for item in incoming:
        if item['id'] not in processed_file_sources:
            name = secrets.token_urlsafe() + '.pdf'
            print(f'Copying {item["name"]} to {name}')
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

    # print(processed)

    # OCR those which need OCRing
    for item in processed:
        copiedFrom = item.get('appProperties', {}).get('copiedFrom')
        if copiedFrom is None or item['mimeType'] != 'application/pdf':
            continue

        ocrTextFileId = item.get('appProperties', {}).get('ocrTextFileId')
        if ocrTextFileId is not None:
            continue

        basename = '.'.join(item['name'].split('.')[:-1])

        print(f'Downloading and OCR-ing {item["name"]}')
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
            with open(text_path, 'wb') as fobj:
                fobj.write(text)

            print("Uploading text")
            media = MediaFileUpload(text_path, mimetype='text/plain')
            text_file = service.files().create(
                body={
                    'name': basename + '.txt',
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

    # Grep text values
    for item in processed:
        pdfSourceFileId = item.get('appProperties', {}).get('pdfSourceFileId')
        if pdfSourceFileId is None or item['mimeType'] != 'text/plain':
            continue

        ucasPersonalId = item.get('appProperties', {}).get('ucasPersonalId')
        if ucasPersonalId is not None:
            continue

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
                matched_personal_ids = [
                    m.group(1)
                    for m in (
                        UCAS_PERSONAL_ID_PATTERN.search(line)
                        for line in fobj.readlines()
                    )
                    if m
                ]

                count_table = {}
                for id in matched_personal_ids:
                    count_table[id] = 1 + count_table.get(id, 0)

                matches_by_count = sorted(
                    list(count_table.items()),
                    key=lambda v: -v[1]
                )

                if len(matches_by_count) == 0:
                    print('ERROR: NO MATCHES!')
                    continue

                if matches_by_count[0][1] < 3:
                    print('ERROR: Too few matches')
                    continue

                id = matches_by_count[0][0]

                service.files().update(
                    fileId=pdfSourceFileId,
                    supportsTeamDrives=True,
                    body={
                        'appProperties': {
                            'ucasPersonalId': id,
                        },
                    }
                ).execute()


#    results = service.teamdrives().list().execute()
#    print(results)


def list_all_files(service, **kwargs):
    files = []
    pageToken = None

    while True:
        results = service.files().list(pageToken=pageToken, **kwargs).execute()
        files.extend(results.get('files', []))

        pageToken = results.get('nextPageToken', '')
        if pageToken == '':
            break

    return files


if __name__ == '__main__':
    main()
