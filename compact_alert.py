"""Single plain-text Telegram job notification. No descriptions or metadata."""
import re
from urllib.parse import quote,urlsplit

def line(value):return re.sub(r'[\s\x00-\x1f\x7f]+',' ',str(value or '')).strip()

def alert(record):
    company=line(record.get('company'));title=line(record.get('title'))
    location=line(record.get('location'))
    if location.casefold() in {'','unknown','not specified','location unknown','nan','none','not stated','n/a','null','undefined','unspecified','not available','not provided'}:location='Location not specified'
    url=None
    for candidate in [record.get('direct_url'),record.get('url')]:
        if not candidate:continue
        candidate=str(candidate).strip();parsed=urlsplit(candidate)
        if parsed.scheme in {'https','http'} and parsed.hostname and not parsed.username and not parsed.password:
            url=candidate;break
    if not url:raise ValueError('Posting URL unavailable or invalid')
    url=quote(url,safe=":/?#[]@!$&'()*+,;=%")
    message=f'{company} \u2014 {title}\n{location}\n{url}'
    if len(message.encode('utf-16-le'))//2>4096:raise ValueError('Compact notification exceeds Telegram limit; pending retained')
    return message
