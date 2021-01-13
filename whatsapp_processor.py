#!/usr/bin/env python3

import argparse
import collections
import datetime
import hashlib
import io
import json
import logging
import os
import pickle
import re
import shutil
from typing import List, Dict

from dateutil import parser as dt_parser
from googleapiclient.discovery import build, Resource
from googleapiclient.http import MediaIoBaseDownload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2 import service_account

# If modifying these scopes, delete the file token.pickle.
SCOPES = ('https://www.googleapis.com/auth/drive.readonly',)
MSG_DELETED = "This message was deleted"
MEDIA_OMITTED = "<Media omitted>"
SKIP_MSGS = (MSG_DELETED, MEDIA_OMITTED)
ACTION_LINE = re.compile(r"(?P<day>[0-9]+/[0-9]+/[0-9]+), (?P<tm>[0-9]+:[0-9]+ (am|pm)) - (?P<tail>[^:]+)$", re.IGNORECASE)
MSG_LINE = re.compile(r"(?P<day>[0-9]+/[0-9]+/[0-9]+), (?P<tm>[0-9]+:[0-9]+ (am|pm)) - (?P<sn>[^:]+): (?P<tail>.*?)$", re.IGNORECASE)
FILE_ATTACHED_RE = re.compile(r"(?P<fn>.*?) \(file attached\)")
GDRIVE_RE = re.compile(r"(?:https://|)drive\.google\.com/.*?/folders/(?P<drive_id>[a-zA-Z0-9_-]+)")
AWS_BUCKET_RE = re.compile(r"^[a-zA-Z0-9.\-_]{1,255}$")
MINUTES = datetime.timedelta(seconds=60)
GOOGLE_DRIVE = "GOOGLE_DRIVE"
REQ_WHATSAPP_ENV_VARS = (
    'WHATSAPP_DB_USERNAME',
    'WHATSAPP_DB_PASSWORD',
    'WHATSAPP_DB_NAME',
    'WHATSAPP_ALL_FILES_DB_COLLECTION',
    'WHATSAPP_MERGED_MSGS_DB_COLLECTION')
REQ_AWS_ENV_VARS = (
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_BUCKET')
DAY_FMTS = {
    "dmy": "%d/%m/%y",
    "mdy": "%m/%d/%y",
}

# Silence unneccesary google api warnings
# https://github.com/googleapis/google-api-python-client/issues/299
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)


class Msg():
    __slots__ = [
        'dt',
        'sender_id',
        'source_type',
        'source_loc',
        'group_id',
        'content',
        'order',
        'file_idx',
        'file_datetime',
        'has_media',
        'media_file',
        'media_upload_loc',
        'media_mime_type',
    ]

    def __init__(self, **kwargs):
        self.dt = kwargs.pop('dt', None)
        if not self.dt and 'datetime' in kwargs:
            self.dt = datetime.datetime.fromisoformat(kwargs.pop('datetime'))
        self.has_media = kwargs.pop('has_media', False)
        self.sender_id = kwargs.pop('sender_id', "")
        self.group_id = kwargs.pop('group_id', "")
        self.source_type = kwargs.pop('source_type', GOOGLE_DRIVE)
        self.source_loc = kwargs.pop('source_loc', "")
        self.content = kwargs.pop('content', "")
        self.order = kwargs.pop('order', None)
        self.file_idx = kwargs.pop('file_idx', None)
        self.file_datetime = kwargs.pop('file_datetime', None)
        self.media_file = kwargs.pop('media_file', {})
        self.media_upload_loc = kwargs.pop('media_upload_loc', None)
        self.media_mime_type = kwargs.pop('media_mime_type', None)

        # for mongo
        kwargs.pop("_id", "")
        assert not kwargs

    def __repr__(self):
        order = self.order if self.order is not None else "not-ordered"
        return "<Msg %s: %s %s: %s>" % (order, self.dt, self.sender_id[:5],
                                        self.content[:50].replace('\n', '\\n'))

    def __eq__(self, other):
        return (self.dt == other.dt
                and self.sender_id == other.sender_id
                and self.group_id == other.group_id
                and self.content == other.content)

    @staticmethod
    def create(match: re.Match, group_id: str, file_idx: int, source_loc: str,
               okey: str, day_fmt: str):
        day_raw = match['day']
        day = datetime.datetime.strptime(day_raw, DAY_FMTS[day_fmt]).date()
        tm_raw = match['tm']
        tm = dt_parser.parse(tm_raw).time()
        sender_id = match['sn'].strip()
        if okey:
            sender_id = encrypt_string(sender_id, okey, group_id)
        return Msg(
            dt=datetime.datetime.combine(day, tm),
            sender_id=sender_id,
            group_id=group_id,
            source_loc=source_loc,
            content=match['tail'],
            file_idx=file_idx,
        )

    def as_dict(self):
        return {
            'datetime': self.dt.isoformat(),
            'source_type': self.source_type,
            'source_loc': self.source_loc,
            'sender_id': self.sender_id,
            'group_id': self.group_id,
            'content': self.content,
            'order': self.order,
            'has_media': self.has_media,
            'media_upload_loc': self.media_upload_loc,
            'media_mime_type': self.media_mime_type,
        }

    @staticmethod
    def from_dict(d):
        return Msg(**d)

    def is_original(self):
        return self.content not in SKIP_MSGS

    def add_content_line(self, content_line: str):
        self.content += '\n' + content_line

    def set_order(self, order: int):
        self.order = order

    def set_file_datetime(self, file_datetime: datetime.datetime):
        self.file_datetime = file_datetime

    def make_media_msg(self, media_file: dict):
        self.has_media = True
        self.media_file = media_file or {}

    def process_media_msg(self):
        """
        After we've downloaded the media message, we can process it
        """
        if self.media_file:
            self.media_upload_loc = self.media_file.get('hash')
            self.media_mime_type = self.media_file['mimeType']

    def merge(self, other):
        """
        Given two Msg objects, merge the one with better content
        """
        assert self.sender_id == other.sender_id
        assert self.group_id == other.group_id

        content_msg = sorted([self, other], key=content_sort)[-1]

        return Msg(
            dt=content_msg.dt,
            sender_id=self.sender_id,
            group_id=self.group_id,
            source_type=content_msg.source_type,
            source_loc=content_msg.source_loc,
            content=content_msg.content,
            file_datetime=content_msg.file_datetime,
            has_media=content_msg.has_media,
            media_file=content_msg.media_file,
            media_upload_loc=content_msg.media_upload_loc,
            media_mime_type=content_msg.media_mime_type,

        )


def content_sort(msg: Msg) -> tuple:
    """
    Which content is best?
    content_sort[0] will be content which is NOT deleted first
    a.k.a True > False
    """
    return (msg.is_original(),
            msg.has_media,
            bool(msg.media_upload_loc),
            bool(msg.media_file))


def get_gdrive_service(creds_path: str) -> Resource:
    """
    Get the google drive service client (aka the 'Resource')

    Primarily copied from Google Drive tutorial:
    https://developers.google.com/drive/api/v3/quickstart/python
    """

    assert os.path.exists(creds_path)
    with open(creds_path) as f:
        jobj = json.loads(f.read())
        is_service_account = jobj.get('has_media') == 'service_account'

    if is_service_account:
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)

    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            logging.info("Logging in via oauth")
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('drive', 'v3', credentials=creds)


def get_files_from_drive(drive_id: str, gdrive_service: Resource) -> list:
    """
    Pull files from a specific google drive file
    """
    files = []
    page_token = None
    while True:
        try:
            param = {'q': f'"{drive_id}" in parents'}
            if page_token:
                param['pageToken'] = page_token
            gdrive_resp = gdrive_service.files().list(**param).execute()
            files += gdrive_resp['files']
            page_token = gdrive_resp.get('nextPageToken')
            if not page_token:
                break
        except Exception as ex:
            logging.error('An error occurred: %s', ex)
            break
    return files


def separate_text_and_media_files(files: list) -> (list, list):
    """
    Text and media files are processed differently so separate them.
    """
    text_files = []
    media_files = []
    for fl in files:
        if fl['mimeType'] == 'text/plain' and \
           fl['name'].startswith("WhatsApp Chat with "):
            text_files.append(fl)
        else:
            media_files.append(fl)
    return text_files, media_files


def download_content_to_file(file_dict: dict, gdrive_service: Resource):
    """
    Download the file content from S3. This modifies the file dict in-place.
    """

    file_id = file_dict['id']

    request = gdrive_service.files().get_media(fileId=file_id)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        _, done = downloader.next_chunk()
    fh.seek(0)
    file_dict['content'] = fh
    logging.info("Downloaded %r (%s).", file_id, file_dict['mimeType'])


def encrypt_string(string: str, salt1: str, salt2="") -> str:
    """
    Returns an encrypted string for anonymization of groups and names / phones
    """
    salt = salt1 + salt2
    return hashlib.pbkdf2_hmac('sha256', string.encode(),
                               salt.encode(), 1).hex()


def process_text_file(text_file: dict, media_files_by_name: dict, file_idx: int,
                      source_loc: str, day_fmt: str, okey: str) -> list:
    """
    Given a whatsapp message thread text file, break that file into individual
    messages which are suitable for upload to mongo.
    """
    # 1. Build up the messages line-by-line
    group_id = text_file['name']
    if okey:
        group_id = encrypt_string(group_id, okey)
    msgs = []
    content_lines = text_file['content'].read().decode().split('\n')
    current_msg = None
    for content_line in content_lines:
        if ACTION_LINE.match(content_line):
            # action header is a subset of message header but if we get it,
            # it means the message is over and we should save the message
            if current_msg:
                msgs.append(current_msg)
                current_msg = None
        if msg_match := MSG_LINE.match(content_line):
            if current_msg:
                msgs.append(current_msg)
            current_msg = Msg.create(msg_match, group_id, file_idx, source_loc,
                                     okey, day_fmt)
            continue
        if current_msg:
            current_msg.add_content_line(content_line)
    if current_msg:
        msgs.append(current_msg)

    # 2. With all the messages, do some processing on each
    if msgs:
        file_datetime = msgs[-1].dt
        for i, msg in enumerate(msgs):
            msg.set_order(i)
            msg.content = msg.content.strip()
            msg.set_file_datetime(file_datetime)
            if attach_match := FILE_ATTACHED_RE.match(msg.content):
                media_file = media_files_by_name.get(attach_match['fn'])
                msg.make_media_msg(media_file)
    logging.info("Processed WhatsApp group %r", group_id)
    return msgs


def filter_superfluous_media_files(media_files: list, media_msgs: list) -> list:
    """
    Some media files are not referenced in any messages. I don't know why.
    Filter these.
    """

    filtered_media_files = []
    for media_file in media_files:
        if any(media_file['name'] == m.media_file.get('name')
               for m in media_msgs):
            filtered_media_files.append(media_file)
    logging.info("Filtered out %d/%s media files",
                 len(media_files) - len(filtered_media_files), len(media_files))
    return filtered_media_files


def save_to_local(drive_id: str, all_msgs: List[Msg], msgs_to_insert: List[Msg],
                  media_files: List[dict], skip_media: bool) -> None:
    """
    Save messages and media to the filesystem.
    """
    today = datetime.date.today().isoformat().replace('-', '_')

    fn = f"all_msgs_{today}_{drive_id}.json"
    with open(fn, 'w') as f:
        f.write(json.dumps([m.as_dict() for m in all_msgs]))
        logging.info("Wrote %d messages to %r", len(all_msgs), fn)

    fn = f"merged_msgs_{today}_{drive_id}.json"
    with open(fn, 'w') as f:
        f.write(json.dumps([m.as_dict() for m in msgs_to_insert]))
        logging.info("Wrote %d messages to %r", len(msgs_to_insert), fn)

    if skip_media:
        return

    media_dir = f"msg_media_{today}_{drive_id}"
    if os.path.exists(media_dir):
        if input("Overwrite media directory %r? " % media_dir)[0] == 'y':
            shutil.rmtree(media_dir)
        else:
            logging.warning("Media files will not be saved. Done.")
            return
    os.makedirs(media_dir)
    for media_file in media_files:
        path = os.path.join(media_dir, media_file['hash'])
        with open(path, 'wb') as f:
            f.write(media_file['content'].read())
    logging.info("Wrote %d media files to %r. Done", len(media_files), media_dir)


def group_msgs(msgs: List[Msg]) -> Dict[str, List[Msg]]:
    """
    Group messages by their group_id & drive_id and return sorted
    """
    msgs_by_group = collections.defaultdict(list)
    for msg in msgs:
        group_key = (msg.source_type, msg.source_loc, msg.group_id)
        msgs_by_group[group_key].append(msg)
    for msgs_in_group in msgs_by_group.values():
        msgs_in_group.sort(key=msg_sort)
    return dict(msgs_by_group)


def msg_sort(msg: Msg) -> tuple:
    """
    When sorting messages, the lowest datetime always comes first.
    In the case of the tie, we rely on the fact that we have calculated the order.
    In the case of another tie, prefer the message that was NOT deleted
    """
    return (msg.dt, msg.order, msg.content == MSG_DELETED)


def merge_msgs_given_offset(msgs_a: List[Msg], msgs_b: List[Msg],
                            offset: int) -> List[Msg]:
    """
    Given an offset, merge the two lists of messages into one list with no dups
    """
    assert msgs_a[0].dt <= msgs_b[0].dt
    ret = []
    i = -1 * offset - 1
    while True:
        i += 1
        msg_a, msg_b = None, None
        try:
            msg_a = msgs_a[i + offset]
        except IndexError:
            pass
        try:
            if i >= 0:
                msg_b = msgs_b[i]
        except IndexError:
            pass
        if msg_a and msg_b:
            ret.append(msg_a.merge(msg_b))
            continue
        if msg_a:
            ret.append(msg_a)
            continue
        if msg_b:
            ret.append(msg_b)
            continue
        return ret


def check_match(msgs_a: List[Msg], msgs_b: List[Msg], offset: int):
    """
    Determine whether the overlap (offset) is correct for these two lists.
    Returns True when 20 things match in a row
    Returns the number of matches if between 3-20 matches
    Everything else is return False - not a match
    """
    matches = 0
    for i in range(-1 * offset, len(msgs_a) + len(msgs_b)):
        try:
            if i + offset < 0:
                continue
            msg_a = msgs_a[i + offset]
        except IndexError:
            continue
        try:
            if i < 0:
                continue
            msg_b = msgs_b[i]
        except IndexError:
            continue

        if msg_a.sender_id != msg_b.sender_id:
            return False
        if abs((msg_a.dt - msg_b.dt).total_seconds()) > 61:
            return False

        if not msg_a.is_original() or not msg_b.is_original():
            continue

        if msg_a.content != msg_b.content:
            return False

        matches += 1
        if matches > 20:
            return True

    alt_min_match_len = min(len(msgs_a), len(msgs_b)) // 2
    if matches >= 3 or matches >= alt_min_match_len:
        return matches
    return False


def find_offset(msg_set_a: List[Msg], msg_set_b: List[Msg]) -> int:
    """
    Find the offset for the two lists of messages that makes them
    overlap. Raise an assertion error if it can't be done
    """
    checked_offsets = set()
    possible_matches = {}
    for msg_a in msg_set_a:
        if msg_set_b[0].dt - msg_a.dt > 1 * MINUTES:
            continue
        for msg_b in msg_set_b:
            if msg_b.dt - msg_a.dt > 1 * MINUTES:
                continue
            if msg_a.dt - msg_b.dt > 1 * MINUTES:
                break
            offset = msg_a.order - msg_b.order
            if offset in checked_offsets:
                continue
            match_score = check_match(msg_set_a, msg_set_b, offset)
            if match_score is True:
                return offset
            if match_score and isinstance(match_score, int):
                possible_matches[offset] = match_score
            checked_offsets.add(offset)
    if possible_matches:
        return max(possible_matches.keys(),
                   key=lambda o: possible_matches[o])
    raise AssertionError("The two sets of messages do not overlap")


def merge_two_msg_lists(msgs_a: List[Msg], msgs_b: List[Msg]) -> List[Msg]:
    """
    Given two groups of messages, which are from the same group, merge into one
    list of messages
    """

    # Basic check. We can't merge if the orders within each list are not consistent
    assert len(set(m.order for m in msgs_a)) == len(msgs_a)
    assert len(set(m.order for m in msgs_b)) == len(msgs_b)

    # The code ahead assumes msgs_a is always < msgs_b. If they aren't, swap them
    if msgs_a[0].dt > msgs_b[0].dt:
        msgs_a, msgs_b = msgs_b, msgs_a

    if msgs_a[-1].dt < msgs_b[0].dt:
        # If the messages don't overlap in dates, return them concatenated
        merged = msgs_a + msgs_b
    else:
        # Else, do a more complicated thing
        offset = find_offset(msgs_a, msgs_b)
        merged = merge_msgs_given_offset(msgs_a, msgs_b, offset)
    for i, msg in enumerate(merged):
        msg.set_order(i)
    return merged


def group_by_file(msgs: List[Msg]) -> Dict[int, List[Msg]]:
    """
    Group messages by file and return sorted
    """
    msgs.sort(key=msg_sort)
    msgs_by_file = collections.defaultdict(list)
    for msg in msgs:
        msgs_by_file[msg.file_idx].append(msg)
    return dict(msgs_by_file)


def merge_msgs_in_group(group_id: str, msgs_in_grp: list) -> list:
    """
    We often get multiple sets of messages from the same group.
    For example, if two text files are in one dump or if we need to update
    the mongo file.
    Merge these.
    This function is complicated so there are plenty of comments.
    """

    # 0. Assert only one group came in
    assert len(set(m.group_id for m in msgs_in_grp)) == 1

    # 1. Sort messages and bucket them by file_idx
    msgs_by_file = group_by_file(msgs_in_grp)
    msgs_by_file = list(msgs_by_file.values())
    num_files = len(msgs_by_file)
    min_msgs_by_file = min(len(msgs) for msgs in msgs_by_file)
    max_msgs_by_file = max(len(msgs) for msgs in msgs_by_file)
    avg_msgs_by_file = sum(len(msgs) for msgs in msgs_by_file) / num_files

    # 2. Return if only one file came in
    if num_files == 1:
        logging.info("Only one file in group %r. No need to merge.", group_id)
        assert set(m.order for m in msgs_in_grp) == set(range(len(msgs_in_grp)))
        return msgs_in_grp

    # 3. Iteratively merge different files
    logging.info("Merging %d files from group %r...", num_files, group_id)
    ret = msgs_by_file.pop()
    while msgs_by_file:
        other_msgs = msgs_by_file.pop()
        ret = merge_two_msg_lists(ret, other_msgs)

    # 4. Final asserts
    unique_content_in = set(m.content for m in msgs_in_grp if m.is_original())
    unique_content_out = set(m.content for m in ret if m.is_original())
    missed = unique_content_in - unique_content_out
    if missed:
        raise AssertionError("Missed content %r" % missed)

    assert set(m.order for m in ret) == set(range(len(ret)))

    logging.info("Merged %d files [min_num_msgs:%d avg_num_msgs:%d "
                 "max_num_msgs:%d] to %d messages",
                 num_files,
                 min_msgs_by_file,
                 avg_msgs_by_file,
                 max_msgs_by_file,
                 len(ret))
    return ret


def merge_all_msgs(msgs: list) -> list:
    """
    We could easily get multiple files from the same group. Merge these.
    """

    msgs_by_group = group_msgs(msgs)
    ret = []
    for group_key, msgs_in_group in msgs_by_group.items():
        ret += merge_msgs_in_group(group_key[2], msgs_in_group)
    return ret


def set_media_hash(media_file: dict) -> None:
    """
    Set the hash so that we can track content over time
    """
    media_content = media_file['content'].read()
    media_file['hash'] = hashlib.sha256(media_content).hexdigest()
    media_file['content'].seek(0)


def process_whatsapp(creds_path: str, google_drive_url: str, day_fmt: str,
                     okey: str, skip_media: bool) -> None:
    """
    Seven steps to download a WhatsApp dump, extract messages,
    and save messages and media.
    """

    # 1. Seturivep google d
    drive_url_match = GDRIVE_RE.match(google_drive_url)
    if not drive_url_match:
        logging.error("Invalid google drive url %r", google_drive_url)
        if 'folder' not in google_drive_url:
            logging.error("Use the containing folder url. Put your export "
                          "in a folder if not already there.")
        exit(1)
    drive_id = drive_url_match['drive_id']
    gdrive_service = get_gdrive_service(creds_path)

    # 2. Download file dictionaries from google drive
    files = get_files_from_drive(drive_id, gdrive_service)
    if not files:
        logging.warning("Found 0 files at %r. Check your url/credentials.",
                        drive_id)
        exit(1)
    text_files, media_files = separate_text_and_media_files(files)
    logging.info("Retrieved %d files (%d text %d media) files from %r",
                 len(files), len(text_files), len(media_files), drive_id)

    # 3. Prepare media file dicts
    media_files_by_name = {afd['name']: afd for afd in media_files}

    # 4. Download whatsapp text contents and extract individual messages
    msgs = []
    for file_idx, text_file in enumerate(text_files):
        download_content_to_file(text_file, gdrive_service)
        msgs += process_text_file(text_file, media_files_by_name,
                                  file_idx, drive_id, day_fmt, okey)
    media_msgs = [m for m in msgs if m.has_media]
    logging.info("Processed %d msgs (%d with media)",
                 len(msgs), len(media_msgs))

    # 5. Download media files that are referenced in a message
    media_files = filter_superfluous_media_files(media_files, media_msgs)
    if skip_media:
        logging.warning("Skipped download of %d media files.", len(media_files))
        media_files = []
    else:
        logging.info("Downloading %d media files...", len(media_files))
        for media_file in media_files:
            download_content_to_file(media_file, gdrive_service)
        for media_file in media_files:
            set_media_hash(media_file)
        for media_msg in media_msgs:
            media_msg.process_media_msg()

    # 6. Merge messages from identical files together
    msgs_to_insert = merge_all_msgs(msgs)

    # 7. Save
    save_to_local(drive_id, msgs, msgs_to_insert, media_files, skip_media)


if __name__ == '__main__':
    parser = argparse.ArgumentParser("Tattle WhatsApp processor. See README.md")
    parser.add_argument('credentials',
                        help='Either drive_api or service account credentials')
    parser.add_argument('google_drive_url',
                        help='Google Drive directory of WhatsApp dump')
    parser.add_argument('date_format',
                        choices=['dmy', 'mdy'],
                        help='Determined by which locale you export your '
                             'conversations from. Either day/month/year (dmy) '
                             'or month/day/year (mdy)')
    parser.add_argument('--skip-media', action='store_true',
                        help="Skip downloading / uploading media files")
    parser.add_argument('--obfuscation-key',
                        help="Obfuscation key. Best as a random string. "
                             "If not provided, names, phone numbers, and "
                             "group names will not be obfuscated.")
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    process_whatsapp(args.credentials, args.google_drive_url, args.date_format,
                     args.obfuscation_key, args.skip_media)
