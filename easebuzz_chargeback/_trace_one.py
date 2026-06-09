"""Trace why the bot did not auto-reply on a specific transaction id."""
import sys, re, yaml
from email.utils import getaddresses
from pathlib import Path
import gmail_client as gm
import body_parser as bp

HERE = Path(__file__).parent
TX = sys.argv[1] if len(sys.argv) > 1 else "E2604290XRWSHM"
LEVELS = [
    ("L1", "config.yaml"),
    ("L2", "config_l2.yaml"),
    ("L3", "config_l3.yaml"),
]

cfg = yaml.safe_load((HERE / "config.yaml").read_text())
client = gm.make_client(cfg, base_dir=HERE)
matches = client.search(f"subject:{TX}")
print(f"matches for subject:{TX}  ->  {len(matches)}")
if not matches:
    sys.exit(0)

# pick first thread
msg = client.get_message(matches[0]["id"])
tid = msg["threadId"]
th = client.get_thread(tid)
print(f"\nthread {tid}  msgs={len(th['messages'])}")
print(f"label_ids on first msg: {th['messages'][0].get('labelIds', [])}")

# fetch labels to translate ids
lbls = client.svc.users().labels().list(userId="me").execute().get("labels", [])
id2name = {l["id"]: l["name"] for l in lbls}

for i, m in enumerate(th["messages"]):
    print(f"\n  msg {i}:")
    for h in ["From", "To", "Cc", "Subject", "Reply-To", "Date"]:
        print(f"    {h:10s}: {gm.header(m, h)!r}")
    raw_labels = m.get("labelIds", [])
    named = [id2name.get(l, l) for l in raw_labels]
    print(f"    labels    : {named}")

# subject regex check across all three configs
subj = gm.header(th["messages"][0], "Subject")
print(f"\nSubject: {subj!r}")
for level, cfgpath in LEVELS:
    cc = yaml.safe_load((HERE / cfgpath).read_text())
    pat = cc.get("subject_regex")
    eq  = cc.get("extra_query", "")
    m   = re.search(pat, subj, re.IGNORECASE) if pat else None
    after_m = re.search(r"after:(\S+)", eq)
    print(f"  {level}: regex {'MATCH' if m else 'no match'}   after={after_m.group(1) if after_m else '?'}")

# print first message's full plain-text body
import base64
def find_plain(p, holder):
    if p.get("mimeType") == "text/plain":
        d = p.get("body", {}).get("data")
        if d and not holder[0]:
            holder[0] = base64.urlsafe_b64decode(d.encode()+b"==").decode(errors="replace")
    for sp in p.get("parts", []) or []:
        find_plain(sp, holder)
holder = [""]
find_plain(th["messages"][0].get("payload", {}), holder)
print(f"\n--- first msg plain body (first 800 chars) ---")
print(holder[0][:800])
