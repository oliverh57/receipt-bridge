import os, re, sys, urllib.request, urllib.parse
"""Fetch each bank's own icon from its official website into data/bank-logos/.
They are fetched locally rather than shipped with the app. Re-run to refresh.
Banks that block the request (Revolut, Bank of Scotland) just keep the UI's monogram."""
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bank-logos")
os.makedirs(OUT, exist_ok=True)
SITES = {
 "mettle":"mettle.co.uk","natwest":"natwest.com","rbs":"rbs.co.uk","ulster":"ulsterbank.ie",
 "bankofscotland":"bankofscotland.co.uk","bankofireland":"bankofireland.com","barclays":"barclays.co.uk",
 "caterallen":"caterallen.co.uk","danske":"danskebank.co.uk","firstdirect":"firstdirect.com",
 "halifax":"halifax.co.uk","hsbc":"hsbc.co.uk","lloyds":"lloydsbank.com","metro":"metrobankonline.co.uk",
 "santander":"santander.co.uk","starling":"starlingbank.com","tide":"tide.co","tsb":"tsb.co.uk",
 "virgin":"virginmoney.com","wise":"wise.com","monzo":"monzo.com","revolut":"revolut.com",
 "capitalontap":"capitalontap.com","allica":"allica.bank"}
UA = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15","Accept-Language":"en-GB"}
def get(url, t=15):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t)
    return r.read(), r.geturl(), r.headers.get("Content-Type","")
def candidates(domain):
    cands = []
    try:
        html, final, _ = get("https://www."+domain+"/")
    except Exception:
        try: html, final, _ = get("https://"+domain+"/")
        except Exception as e: return [], str(e)
    html = html.decode("utf8","ignore")
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel = re.search(r'rel=["\']([^"\']+)', tag, re.I); href = re.search(r'href=["\']([^"\']+)', tag, re.I)
        if not rel or not href or "icon" not in rel.group(1).lower(): continue
        if "mask" in rel.group(1).lower(): continue
        sz = re.search(r'sizes=["\'](\d+)x', tag, re.I)
        size = int(sz.group(1)) if sz else (180 if "apple" in rel.group(1).lower() else 0)
        h = href.group(1)
        if h.startswith("data:"): continue
        cands.append((size, urllib.parse.urljoin(final, h)))
    for p in ("/apple-touch-icon.png","/favicon.ico"):
        cands.append((1 if "ico" in p else 150, urllib.parse.urljoin(final, p)))
    cands.sort(key=lambda c: -c[0])
    return cands, ""
for key, dom in SITES.items():
    cands, err = candidates(dom)
    done = False
    for size, url in cands:
        try:
            data, _, ct = get(url)
        except Exception: continue
        if len(data) < 100 or b"<html" in data[:200].lower(): continue
        ext = ".svg" if (b"<svg" in data[:500] or "svg" in ct) else ".ico" if (url.split("?")[0].endswith(".ico") or "icon" in ct and data[:4]==b"\0\0\1\0") else ".png" if data[:4]==b"\x89PNG" else ".jpg" if data[:2]==b"\xff\xd8" else None
        if not ext: continue
        open(f"{OUT}/{key}{ext}","wb").write(data)
        print(f"{key:15} {ext} {len(data):7}B  declared {size}  {url}"); done = True; break
    if not done: print(f"{key:15} FAILED {err}")
