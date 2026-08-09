#!/usr/bin/env python3
"""Discover visible data.gouv.ci dataset URLs; preserve the official catalog as source of truth."""
import html,re,urllib.request
from urllib.parse import urljoin
BASE='https://data.gouv.ci/datasets'
UA='IvoireData/0.2 (+https://github.com/bozz33/IvoireData)'
req=urllib.request.Request(BASE,headers={'User-Agent':UA})
with urllib.request.urlopen(req,timeout=60) as r: body=r.read().decode('utf-8','replace')
links=set()
for href in re.findall(r'href=["\']([^"\']+)["\']',body):
    u=urljoin(BASE,html.unescape(href))
    if u.startswith('https://data.gouv.ci/datasets/'):
        links.add(u.split('#')[0])
for u in sorted(links): print(u)
print(f'# visible catalog links discovered: {len(links)}')
