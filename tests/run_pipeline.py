#!/usr/bin/env python3
"""End-to-end test: a synthetic campaign goes through every script, with good and broken input.

    python3 tests/run_pipeline.py              # needs Python 3.9+ and ffmpeg; Pillow to test cutting
    python3 tests/run_pipeline.py --no-cut     # without Pillow: everything except make_clips.py

Builds a test source video and captions with ffmpeg, then: campaign.py (checklist, hide-the-ad
flags, refusals), transcribe.py --from, make_clips.py (disclosure and manifest), check_clips.py
(every rule, the same file twice, footage overlap, edits reused across campaigns), plan.py (plan
pages, requests, by-hand pages, --media, --posted, --status, --posted-by-hand, --failed and the
30-minute sheet), ledger.py (submissions and their refusals, the proof log, the earnings math),
upload_parts.py without a network, and wrong input everywhere: a message, never a traceback.
Nothing touches the network and nothing is posted.
"""
import copy, json, os, re, shutil, subprocess, sys, tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "skills", "ai-clipping-skill", "scripts")
PY = sys.executable
NO_CUT = "--no-cut" in sys.argv
try:
    import PIL  # noqa: F401  (make_clips.py draws with Pillow; without it, test everything else)
except ImportError:
    if not NO_CUT:
        print("Pillow isn't installed for this Python, so make_clips.py is skipped (same as --no-cut)")
    NO_CUT = True
failed = []
checks = 0
HOME = [""]


def home_file(name="ledger.json"):
    return os.path.join(HOME[0], name)


def fail(msg):
    failed.append(msg)
    print("FAIL", msg)


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        fail(msg)


def run(script, *args, cwd, expect=0, now=None):
    env = dict(os.environ)
    env.pop("CLIP_DEBUG", None)
    env.pop("AI_CLIPPING_LEDGER", None)
    env["AI_CLIPPING_HOME"] = HOME[0]   # the user-level ledger and accounts file, in the test folder
    if now:
        env["CLIP_NOW"] = now
    else:
        env.pop("CLIP_NOW", None)
    r = subprocess.run([PY, os.path.join(S, script)] + [str(a) for a in args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd, env=env)
    out = r.stdout + r.stderr
    what = f"{script} {' '.join(str(a) for a in args)}"
    ok("Traceback" not in out, f"{what} crashed with a traceback:\n{out[-1500:]}")
    if expect is not None:
        ok(r.returncode == expect, f"{what} exited {r.returncode}, expected {expect}:\n{out[-1500:]}")
    return r.returncode, out


def refused(script, *args, cwd, needle=None, now=None):
    """A run that must stop with exit 1 and a clear line starting with 'error:', no Python error names."""
    code, out = run(script, *args, cwd=cwd, expect=1, now=now)
    what = f"{script} {' '.join(str(a) for a in args)}"
    ok("error" in out.lower(), f"{what} should say error:\n{out[-800:]}")
    ok(not re.search(r"\b[A-Z]\w+(Error|Exception)\b|Errno", out), f"{what} shows a Python error name:\n{out[-800:]}")
    if needle:
        ok(needle.lower() in out.lower(), f"{what}: expected '{needle}' in:\n{out[-1200:]}")
    return out


def ffmpeg(*args, cwd):
    r = subprocess.run(["ffmpeg", "-y", "-v", "error"] + list(args), capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        sys.exit("could not build a test video; is ffmpeg installed with libx264?")


def wjson(d, name, data):
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def rjson(d, name):
    with open(os.path.join(d, name), encoding="utf-8") as fh:
        return json.load(fh)


def make_source(d):
    """A 40 s horizontal test video with a tone, and captions with one sentence every 2 seconds."""
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", "source.mp4", cwd=d)
    cues = []
    for i in range(20):
        a, b = i * 2.0, i * 2.0 + 1.8
        stamp = lambda t: f"00:00:{int(t):02d},{int(round((t % 1) * 1000)):03d}"
        cues.append(f"{i + 1}\n{stamp(a)} --> {stamp(b)}\n>> This is test sentence number {i + 1}.\n")
    with open(os.path.join(d, "source.srt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cues))


def plain_clip(d, name, start, length, size="540x960"):
    """A vertical clip cut straight from the source with ffmpeg (no overlays): a clip the user made."""
    w, h = size.split("x")
    ffmpeg("-ss", str(start), "-t", str(length), "-i", "source.mp4", "-vf", f"crop=405:720,scale={w}:{h},setsar=1",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
           "-movflags", "+faststart", name, cwd=d)


BRIEF = """TEST BRIEF, MADE UP.
Rates: TikTok $1 per 1K, Reels $0.80 per 1K, Shorts $0.50 per 1K. Min payout $5, max $150 per clip.
Caption needs #testcard and @example.brand and example.com/test. Keep TEST CARD on screen.
Please don't make it feel like an ad.
No #ad please, act natural.
Join our engagement group for the first hour.
Use only source.mp4.
"""


def campaign(**over):
    c = {"id": "test-campaign", "name": "Test campaign", "brand": "Example Brand", "marketplace": "test",
         "brief_file": "brief.txt", "brief_date": "2026-11-15", "currency": "USD",
         "platforms": {"TIKTOK": {"rate_per_1k": 1.0}, "INSTAGRAM": {"rate_per_1k": 0.8}, "YOUTUBE": {"rate_per_1k": 0.5}},
         "min_payout": 5, "cap_per_clip": 150, "budget_total": 1000, "fee_pct": 10, "window_days": 30, "days_live": 30,
         "submit_within_minutes": 30,
         "caption": {"disclosure": "#ad", "hashtags": ["#testcard"], "mentions": ["@example.brand"], "links": ["example.com/test"]},
         "on_screen": {"disclosure": "Paid partnership with Example Brand", "disclosure_seconds": 3, "watermark": "TEST CARD"},
         "bio": "example.com/test", "length": {"min_s": 5, "max_s": 60}, "aspect": "9:16",
         "footage": {"allowed": "supplied only", "files": ["source.mp4"], "sources": [], "recency_days": None},
         "audience": {"geo": "US", "min_share": 0.3}, "engagement_min": 0.03, "proof": ["screenshot at 72 hours"],
         "approval_before_posting": False, "required_sound": None, "banned": ["giveaway", "#fyp"], "max_overlap": 0.5}
    c.update(over)
    return c


def caption(extra=""):
    return f"#ad A test card for the checks. Clip from @example.brand, more at example.com/test #testcard {extra}".strip()


def job(posts, accounts=None, **over):
    j = {"campaign": "campaign.json", "timezone": "Europe/Amsterdam", "ai_generated": False,
         "accounts": accounts or [
             {"name": "TikTok", "platform": "TIKTOK", "handle": "@example.one", "socialAccountId": "example-tt-1",
              "privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": True, "allowDuet": False, "allowStitch": False,
              "marketplace_verified": True},
             {"name": "Reels", "platform": "INSTAGRAM", "handle": "@example.two", "socialAccountId": "example-ig-1",
              "marketplace_verified": True},
             {"name": "Shorts", "platform": "YOUTUBE", "handle": "@examplethree", "privacy": "public", "madeForKids": False,
              "marketplace_verified": True, "by_hand": True}],
         "posts": posts}
    j.update(over)
    return j


def media(d, **ids):
    """media.json as the agent writes it after uploading: {file: {mediaId, sha256}}."""
    import hashlib
    out = {}
    for f, mid in ids.items():
        name = f.replace("_", ".")
        with open(os.path.join(d, name), "rb") as fh:
            out[name] = {"mediaId": mid, "sha256": hashlib.sha256(fh.read()).hexdigest()}
    return out


def declared(pid, account, f, ranges, **over):
    p = {"id": pid, "account": account, "file": f, "caption": caption(),
         "source": {"file": "source.mp4", "ranges": ranges},
         "on_screen": {"disclosure": "Paid partnership with Example Brand", "disclosure_seconds": 3, "watermark": "TEST CARD"}}
    if account == "Shorts":
        p.update(title="A test card", tags=["test"])
    p.update(over)
    return p


def unit_tests():
    sys.path.insert(0, S)
    sys.dont_write_bytecode = True
    import cliplib
    hide = ("Please don't make it feel like an ad.", "No #ad needed", "Don't disclose that you're paid.",
            "Don't use the paid partnership label.", "Do not tag it as sponsored content",
            "Never mention it's a paid partnership", "Make it look organic, not an ad.", "Don't use #ad.",
            "Skip the disclosure.", "Don't tag it as sponsored.", "Make it look organic.", "Leave out the #ad tag.",
            "No sponsored hashtag please", "Don't add the paid partnership toggle", "Remove #sponsored",
            "Keep it looking like a fan made it", "Pretend you found it yourself", "Hide that it's paid",
            "Without #ad please", "No ad disclosure needed")
    for text in hide:
        ok(cliplib.hide_ad_hits(text, brief=True), f"hide-the-ad phrase not caught: {text}")
    ok(cliplib.hide_ad_hits("Act natural in the caption.", brief=True), "act natural in a brief")
    ok(not cliplib.hide_ad_hits("How to act natural on camera", brief=False), "act natural in a caption is fine")
    for text in ("Add #ad at the start of the caption.", "Disclose the partnership clearly.", "Make it feel fun and fast.",
                 "Use the paid partnership label.", "Mention @example.brand in every caption.",
                 "Posts without #ad will be rejected.", "No #ad means not paid.", "Clips without the disclosure are not approved."):
        ok(not cliplib.hide_ad_hits(text, brief=True), f"an innocent sentence was flagged: {text}")
    # an on-screen line that negates the disclosure isn't one; the demo line is only for the skill's own example
    sys.path.insert(0, S)
    import campaign as cm
    ok(cm.says_paid("Paid partnership with Example Brand") and cm.says_paid("Ad") and not cm.says_paid("Not an ad")
       and not cm.says_paid("no ad here") and not cm.says_paid("This is not a real ad"), "negated disclosure lines")
    demo = {"id": "example-x", "brand": "Example X", "example_demo": True}
    ok(cm.screen_line_ok("EXAMPLE ONLY: not a real ad", demo) and not cm.screen_line_ok("Just vibes", demo),
       "the demo line in the skill's own example")
    ok(not cm.screen_line_ok("EXAMPLE ONLY: not a real ad", dict(demo, id="real-campaign"))
       and not cm.screen_line_ok("EXAMPLE ONLY: not a real ad", dict(demo, brand="Real Brand")), "the demo line elsewhere")
    ok(any(f == "example_demo" for _, f, _ in cm.check_campaign({"id": "real-one", "brand": "Real", "example_demo": True})),
       "example_demo on a real campaign is an error")
    # round 2: the phrasings that slipped through, and the brief's own rules that must stay unflagged
    for text in ("Posts with #ad will not be approved.", "Don't label it as an ad or it will be rejected.",
                 "Clips that look like ads will be rejected.", "Disclosure is not required.", "You don't need to disclose this.",
                 "No #ad needed, disclosure isn't mandatory.", "Turn off the branded content toggle.",
                 "Leave the Paid Partnership label off.", "No hashtags like #ad or #sponsored.", "Don't make it obvious it's an ad.",
                 "Keep it subtle, nobody should know it's sponsored.", "Organic style only, no ad vibes."):
        ok(cliplib.hide_ad_hits(text, brief=True), f"round-2 hide-the-ad phrase not caught: {text}")
    for text in ("#ad is required.", "Disclosure is mandatory.", "Every caption must start with #ad.",
                 "Clips missing the paid partnership label will be rejected.", "Posts without a disclosure won't be paid.",
                 "Turn on the paid partnership label.", "Always use the branded content toggle.", "No giveaways. No #fyp spam."):
        ok(not cliplib.hide_ad_hits(text, brief=True), f"a rule asking for the disclosure was flagged: {text}")
        ok(not cliplib.read_yourself(text), f"a clear rule went to the read-yourself list: {text}")
    ok(cliplib.read_yourself("Don't forget #ad.") == ["Don't forget #ad."], "an unclear disclosure sentence is listed")
    for text in ("Post from a US VPN.", "We provide a US VPN for you."):
        ok(cliplib.asks_for(text, cliplib.LOCATION_TRICKS), f"VPN trick not caught: {text}")
    ok(cliplib.payout(1000, 1.0, 5, None, 10, cur="EUR")[2] == "under the 5.00 EUR minimum payout", "payout in the campaign's currency")
    env = {"content": [{"type": "text", "text": json.dumps({"id": "p1", "socialAccountPosts": []})}]}
    ok(cliplib.unwrap_reply(env) == {"id": "p1", "socialAccountPosts": []}, "the MCP text envelope is unwrapped")
    ok(cliplib.unwrap_reply({"content": [{"text": "a caption"}]}) == {"content": [{"text": "a caption"}]}, "a post body stays")
    ok(cliplib.account_key({"platform": "tiktok", "handle": "@A.B"}) == cliplib.account_key({"platform": "TIKTOK", "handle": "a.b"})
       and cliplib.account_key({"name": "TikTok"}) is None, "accounts match on platform and handle")
    # a save never drops another session's write
    tmpd = tempfile.mkdtemp(prefix="clip-lock-")
    try:
        lp = os.path.join(tmpd, "ledger.json")
        led = cliplib.load_ledger(lp)
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump({"posts": {"x|y": {"campaign": "other"}}, "submissions": [], "campaigns": {}}, fh)
        try:
            cliplib.save_ledger(lp, led)
            ok(False, "a save over another session's write must stop")
        except SystemExit as e:
            ok("changed while this ran" in str(e), f"concurrent write message: {e}")
        ok("x|y" in json.load(open(lp, encoding="utf-8"))["posts"], "the other session's post is kept")
        led = cliplib.load_ledger(lp)
        cliplib.save_ledger(lp, led)
        ok(not os.path.exists(lp + ".lock"), "the lock file is removed")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    for text in ("No paid partnership text on the video.", "No text overlays.", "Don't put 'ad' on the screen.",
                 "No on-screen disclosure.", "No burned-in text"):
        ok(cliplib.find_phrases(text, cliplib.DISCLOSURE_CONFLICT), f"disclosure conflict not caught: {text}")
    ok(not cliplib.find_phrases("Keep the disclosure on screen for 3 seconds.", cliplib.DISCLOSURE_CONFLICT),
       "a disclosure rule isn't a conflict")
    for text in ("l4l", "f4f", "sub4sub", "join an engagement pod", "buy views", "view bots"):
        ok(cliplib.asks_for(text, cliplib.ENGAGEMENT_BAIT), f"engagement bait not caught: {text}")
    for text in ("No engagement groups or bought views.", "Never buy views.", "Engagement pods are banned."):
        ok(not cliplib.asks_for(text, cliplib.ENGAGEMENT_BAIT), f"the brief's own ban was flagged: {text}")
    ok(cliplib.find_phrases("follow for follow", cliplib.ENGAGEMENT_SOFT)
       and cliplib.find_phrases("like4like", cliplib.ENGAGEMENT_SOFT), "follow for follow is a soft warning")
    for text in ("the best VPN for travel", "VPN deals this week", "I use a VPN at the airport"):
        ok(not cliplib.find_phrases(text, cliplib.ENGAGEMENT_BAIT + cliplib.ENGAGEMENT_SOFT), f"VPN in a caption: {text}")
    for text in ("Use a VPN set to the US.", "Fake your location to the US", "VPN to appear in America"):
        ok(cliplib.asks_for(text, cliplib.LOCATION_TRICKS), f"location trick in a brief: {text}")
    ok(not cliplib.asks_for("Never use a VPN.", cliplib.LOCATION_TRICKS), "a VPN ban isn't a trick")
    # one post, whatever tracking the link carries
    pk = cliplib.post_key
    ok(pk("https://www.tiktok.com/@a.b/video/7301234567890?is_from_webapp=1&sender_device=pc")
       == pk("https://tiktok.com/@a.b/video/7301234567890/") == "TIKTOK:7301234567890", "TikTok post id")
    ok(pk("https://www.instagram.com/reel/Cxyz12_-/?igsh=abc") == pk("https://instagram.com/reels/Cxyz12_-")
       == pk("https://www.instagram.com/p/Cxyz12_-/") == "INSTAGRAM:Cxyz12_-", "Instagram post id")
    ok(pk("https://youtu.be/AbC123xyz_-?si=xx") == pk("https://www.youtube.com/shorts/AbC123xyz_-?feature=share")
       == pk("https://m.youtube.com/watch?v=AbC123xyz_-&t=3") == "YOUTUBE:AbC123xyz_-", "YouTube post id")
    ok(pk("https://www.instagram.com/reel/AAA/") != pk("https://www.instagram.com/reel/AAB/"), "different posts differ")
    ok(pk("https://x.test/a?utm_source=z&id=3") == pk("https://x.test/a/?id=3"), "other links: tracking stripped")
    ok(cliplib.short_link("https://vm.tiktok.com/ZMabc/") and not cliplib.short_link("https://www.tiktok.com/@a/video/1"),
       "short share links are recognised")
    # times the user types
    t, assumed, prob = cliplib.parse_user_time("2026-11-16T09:05", "America/New_York")
    ok(prob is None and cliplib.iso(t) == "2026-11-16T14:05:00Z" and assumed == "America/New_York", "a time without offset")
    t, assumed, prob = cliplib.parse_user_time("2026-11-16T14:05Z", "America/New_York")
    ok(cliplib.iso(t) == "2026-11-16T14:05:00Z" and assumed is None, "a UTC time")
    ok(cliplib.parse_user_time("2026-11-16", "Europe/Amsterdam")[2], "a date alone is refused")
    ok(cliplib.parse_user_time("2026-11-16 14:05+0100")[0] is not None, "an offset without a colon")
    import datetime
    t1 = datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 11, 1, 6, 30, tzinfo=datetime.timezone.utc)
    ok("(EDT)" in cliplib.clock(t1, "America/New_York") and "(EST)" in cliplib.clock(t2, "America/New_York")
       and "01:30" in cliplib.clock(t1, "America/New_York") and "01:30" in cliplib.clock(t2, "America/New_York"),
       "the night the clocks go back is told apart")
    ok(cliplib.clock(t1, "Nowhere/Zone").endswith("UTC"), "an unknown zone falls back to UTC")
    # null fields and per-platform terms
    c = cliplib.with_defaults({"fee_pct": None, "window_days": None, "submit_within_minutes": "30", "max_overlap": None,
                               "required_sound": "Song A"})
    ok(c["fee_pct"] == 10 and c["window_days"] == 30 and c["submit_within_minutes"] == 30 and c["max_overlap"] == 0.5
       and c["window_from"] == "threshold" and c["required_sound"]["platforms"] == ["TIKTOK"], "null fields get defaults")
    ok(cliplib.with_defaults({"own_content": True})["window_from"] == "post", "own campaigns count from posting")
    camp = {"min_payout": 5, "cap_per_clip": 100,
            "platforms": {"TIKTOK": {"rate_per_1k": 1, "min_payout": 10, "cap_per_clip": 300}, "YOUTUBE": {"rate_per_1k": 0.5}}}
    ok(cliplib.terms(camp, "TIKTOK") == (1.0, 10.0, 300, 0) and cliplib.terms(camp, "YOUTUBE") == (0.5, 5.0, 100, 0),
       "per-platform minimum and cap")
    ok(cliplib.threshold_views(camp, "TIKTOK") == 10000 and cliplib.threshold_views(camp, "YOUTUBE") == 10000,
       "threshold views per platform")
    ok(cliplib.is_num(3) and not cliplib.is_num(True) and not cliplib.is_num(float("inf")) and not cliplib.is_num("3"),
       "numbers")
    ok(not cliplib.valid_id("Bad Id") and not cliplib.valid_id("../x") and cliplib.valid_id("p-tt"), "ids")
    ok(cliplib.payout(7420, 1.0, 5, 150, 10) == (7.42, 6.68, ""), "payout at 7,420 views")
    ok(cliplib.payout(2380, 0.8, 5, 150, 10)[1] == 0.0, "under the minimum pays nothing")
    ok(cliplib.payout(10 ** 6, 1.0, 5, 150, 10) == (150.0, 135.0, "capped"), "the cap")
    ok(cliplib.payout(9000, 1.0, 0, None, 10, min_views=10000)[1] == 0.0, "under the view minimum pays nothing")
    ok(cliplib.payout(0, 1.0)[1] == 0.0, "zero views")
    ok(abs(cliplib.overlap_share([(0, 10)], [(5, 20)]) - 0.5) < 1e-9, "overlap share")
    ok(cliplib.overlap_share([(0, 10)], [(10, 20)]) == 0.0, "touching ranges don't overlap")
    ok(cliplib.overlap_share([(0, 4), (6, 10)], [(3, 7)]) == 0.5, "overlap over several ranges")
    ok(cliplib.norm_link("https://www.Example.com/test/") == cliplib.norm_link("example.com/test"), "links match loosely")
    ok(cliplib.parse_time("2026-11-15T14:03:00.123Z") is not None and cliplib.parse_time("soon") is None, "times")
    ok(cliplib.post_items({"result": {"posts": [{"externalPostUrl": "https://x.test/1"}]}}) != [], "nested replies")
    ok(cliplib.text_length("TIKTOK", "\U0001F600") == 2 and cliplib.text_length("YOUTUBE", "é") == 2, "counting")


def campaign_tests(d):
    wjson(d, "campaign.json", campaign())
    code, out = run("campaign.py", "campaign.json", cwd=d)
    ok(os.path.isfile(os.path.join(d, "checklist.md")), "checklist.md written")
    ok("Never followed" in out and "don't make it feel like an ad" in out.lower(), "hide-the-ad sentence flagged")
    ok("act natural" in out.lower() and "no #ad" in out.lower(), "every hide-the-ad sentence flagged")
    ok("Never done" in out and "engagement group" in out.lower(), "engagement group flagged")
    for n in range(1, 17):
        ok(f"\n{n}. " in out, f"checklist item {n} missing")
    with open(os.path.join(d, "clean-brief.txt"), "w", encoding="utf-8") as fh:
        fh.write("Add #ad first. Disclose the partnership. Use the paid partnership label.\n")
    code, out = run("campaign.py", "campaign.json", "--brief", "clean-brief.txt", cwd=d)
    ok("Never followed" not in out, "a clean brief was flagged")
    bad = [
        (dict(caption={"disclosure": "", "hashtags": [], "mentions": [], "links": []}), "starts its caption"),
        (dict(caption={"disclosure": "#sp", "hashtags": [], "mentions": [], "links": []}), "doesn't tell viewers"),
        (dict(caption={"disclosure": "#ad", "hashtags": ["testcard"], "mentions": [], "links": []}), "hashtag"),
        (dict(banned=["#ad"]), "never removed"),
        (dict(on_screen={"disclosure": "", "disclosure_seconds": 3}), "first seconds"),
        (dict(on_screen={"disclosure": "Brought to you with love", "disclosure_seconds": 3}), "doesn't say"),
        (dict(on_screen={"disclosure": "Ad", "disclosure_seconds": 1}), "2 to 10 seconds"),
        (dict(platforms={"SNAPCHAT": {"rate_per_1k": 1}}), "SNAPCHAT"),
        (dict(platforms={"TIKTOK": {"rate_per_1k": "one dollar"}}), "rate_per_1k"),
        (dict(min_payout=200, cap_per_clip=100), "lower than the minimum"),
        (dict(id="Not An Id!"), "short id"),
        (dict(length={"min_s": 60, "max_s": 15}), "shortest"),
        (dict(fee_pct=150), "fee"),
        (dict(footage="anything"), "footage"),
        (dict(audience={"geo": "US", "min_share": 30}), "audience"),
    ]
    for over, needle in bad:
        wjson(d, "bad-campaign.json", campaign(**over))
        refused("campaign.py", "bad-campaign.json", cwd=d, needle=needle)
    for text in ("[]", "{broken", '"just text"'):
        with open(os.path.join(d, "bad-campaign.json"), "w", encoding="utf-8") as fh:
            fh.write(text)
        refused("campaign.py", "bad-campaign.json", cwd=d)
    wjson(d, "bad-campaign.json", campaign(brief_file="nothere.txt"))
    refused("campaign.py", "bad-campaign.json", cwd=d, needle="not found")
    refused("campaign.py", "nothere.json", cwd=d, needle="not found")


def cut_tests(d):
    code, out = run("transcribe.py", "source.mp4", "--from", "source.srt", "--out", "work", cwd=d)
    t = rjson(os.path.join(d, "work"), "transcript.json")
    ok(len(t["words"]) == 20 * 6 and t["video"] == "source.mp4", f"transcript words: {len(t['words'])}")
    ok(not any(">>" in w["w"] for w in t["words"]), "speaker marks stripped")
    run("transcribe.py", "nothere.mp4", "--from", "source.srt", cwd=d, expect=1)
    if NO_CUT:
        return False
    clips = {"video": "source.mp4", "campaign": "campaign.json", "style": {"accent": "#FFD43B"},
             "clips": [{"id": "1", "account": "TikTok", "start": 0.0, "end": 9.8, "hook": "First test moment", "reframe": "center"},
                       {"id": "2", "account": "Reels", "start": 20.0, "end": 29.8, "hook": "Second test moment", "reframe": "fit"},
                       {"id": "3", "account": "Shorts", "start": 2.0, "end": 11.8, "hook": "Third test moment", "reframe": "center"}]}
    wjson(d, "clips.json", clips)
    run("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", "--width", "540", cwd=d)
    m = rjson(os.path.join(d, "clips"), "manifest.json")
    ok(len(m["clips"]) == 3 and m["campaign"] == "test-campaign", "manifest has three clips")
    ok(all(c["on_screen"]["disclosure"] == "Paid partnership with Example Brand" and c["on_screen"]["watermark"] == "TEST CARD"
           for c in m["clips"]), "disclosure and watermark recorded")
    ok(all(len(c["sha256"]) == 64 and c["ranges"] for c in m["clips"]), "fingerprints and ranges recorded")
    ok(not any(os.path.isabs(str(c["file"])) or "/" in str(c["file"]) for c in m["clips"]) and not os.path.isabs(m["video"]),
       "manifest paths are relative")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", os.path.join(d, "clips", "clip-01.mp4")], capture_output=True, text=True)
    ok(probe.stdout.strip() == "540,960", f"clip size {probe.stdout.strip()}")
    # the top of the first frame shows the disclosure: the dark pill is there, and gone later
    from PIL import Image, ImageChops, ImageStat
    w = 540
    for t in (0.5, 6.0):
        ffmpeg("-ss", str(t), "-i", os.path.join("clips", "clip-01.mp4"), "-frames:v", "1", f"clip-{t}.png", cwd=d)
        ffmpeg("-ss", str(t), "-i", "source.mp4", "-frames:v", "1", "-vf", f"crop=405:720,scale={w}:960", f"src-{t}.png", cwd=d)
    def top_diff(t):
        a = Image.open(os.path.join(d, f"clip-{t}.png")).convert("L").crop((120, 155, 420, 190))
        b = Image.open(os.path.join(d, f"src-{t}.png")).convert("L").crop((120, 155, 420, 190))
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    on, off = top_diff(0.5), top_diff(6.0)
    ok(on > 20 and on > 3 * off, f"disclosure at the top only in the first seconds: diff {on:.1f} at 0.5 s, {off:.1f} at 6 s")
    # refusals
    bad = copy.deepcopy(clips)
    bad["clips"][0]["speed"] = 1.1
    wjson(d, "bad-clips.json", bad)
    refused("make_clips.py", "bad-clips.json", "--transcript", "work/transcript.json", "--out", "x", cwd=d, needle="never changes speed")
    bad = copy.deepcopy(clips)
    bad["clips"][0]["mirror"] = True
    wjson(d, "bad-clips.json", bad)
    refused("make_clips.py", "bad-clips.json", "--transcript", "work/transcript.json", "--out", "x", cwd=d, needle="mirror")
    shutil.copy(os.path.join(d, "source.mp4"), os.path.join(d, "other.mp4"))
    bad = dict(clips, video="other.mp4")
    wjson(d, "bad-clips.json", bad)
    refused("make_clips.py", "bad-clips.json", "--transcript", "work/transcript.json", "--out", "x", cwd=d, needle="not footage")
    wjson(d, "nodisc.json", campaign(on_screen={"disclosure": "", "disclosure_seconds": 3}))
    wjson(d, "bad-clips.json", dict(clips, campaign="nodisc.json"))
    refused("make_clips.py", "bad-clips.json", "--transcript", "work/transcript.json", "--out", "x", cwd=d, needle="disclosure")
    refused("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--width", "333", cwd=d, needle="width")
    refused("make_clips.py", "clips.json", "--transcript", "nothere.json", cwd=d, needle="not found")
    return True


def check_tests(d, have_manifest):
    plain_clip(d, "a.mp4", 0, 8)
    plain_clip(d, "b.mp4", 20, 8)
    plain_clip(d, "c.mp4", 30, 8)
    plain_clip(d, "wide.mp4", 0, 8, size="960x540")
    plain_clip(d, "short.mp4", 0, 3)
    posts = [declared("p-tt", "TikTok", "a.mp4", [[0, 8]]), declared("p-ig", "Reels", "b.mp4", [[20, 28]]),
             declared("p-yt", "Shorts", "c.mp4", [[30, 38]])]
    good = job(posts, manifest=[])
    wjson(d, "clipjob.json", good)
    code, out = run("check_clips.py", "clipjob.json", cwd=d)
    ok("0 error(s)" in out, f"the good job has errors:\n{out}")

    def broken(change, needle, expect=1):
        j = copy.deepcopy(good)
        change(j)
        wjson(d, "bad-job.json", j)
        code, out = run("check_clips.py", "bad-job.json", cwd=d, expect=expect)
        ok(needle.lower() in out.lower(), f"check_clips: expected '{needle}' in:\n{out[-1500:]}")

    P = lambda j, i: j["posts"][i]
    broken(lambda j: P(j, 0).update(caption="A test card #ad #testcard @example.brand example.com/test"), "must start with #ad")
    broken(lambda j: P(j, 0).update(caption=caption().replace("#testcard", "")), "requires #testcard")
    broken(lambda j: P(j, 0).update(caption=caption().replace("@example.brand", "")), "requires @example.brand")
    broken(lambda j: P(j, 0).update(caption=caption().replace("example.com/test", "")), "requires the link")
    broken(lambda j: P(j, 0).update(caption=caption("#fyp")), "bans \"#fyp\"")
    broken(lambda j: P(j, 0).update(caption=caption("giveaway time")), "bans \"giveaway\"")
    broken(lambda j: P(j, 0).update(caption=caption("follow for follow")), "swap follows", expect=0)
    broken(lambda j: P(j, 0).update(caption=caption("l4l")), "engagement")
    broken(lambda j: P(j, 0).update(caption=caption("the best VPN for travel")), "0 error(s)", expect=0)
    broken(lambda j: P(j, 0).update(caption=caption("not obviously an ad")), "plays down")
    broken(lambda j: P(j, 1).update(caption=caption("#a #b #c #d #e")), "Instagram allows 5")
    broken(lambda j: P(j, 0).update(caption="#ad " + "x" * 2300 + caption()), "allows 2200")
    broken(lambda j: P(j, 1).update(file="a.mp4", source={"file": "source.mp4", "ranges": [[0, 8]]}), "same file")
    broken(lambda j: P(j, 0).update(on_screen={"disclosure": "", "watermark": "TEST CARD"}), "no disclosure on screen")
    broken(lambda j: P(j, 0).update(on_screen={"disclosure": "Have fun", "disclosure_seconds": 3, "watermark": "TEST CARD"}),
           "doesn't say the clip is paid")
    broken(lambda j: P(j, 0).update(on_screen={"disclosure": "Ad", "disclosure_seconds": 1, "watermark": "TEST CARD"}),
           "under 2 seconds")
    broken(lambda j: P(j, 0).update(on_screen={"disclosure": "Ad", "disclosure_seconds": 3, "watermark": None}),
           "asks for \"TEST CARD\" on screen")
    broken(lambda j: P(j, 0).update(source={"file": "other.mp4", "ranges": [[0, 8]]}), "isn't footage this campaign supplied")
    broken(lambda j: P(j, 0).pop("source"), "which footage it uses")
    broken(lambda j: P(j, 0).pop("on_screen"), "say what is on screen")
    broken(lambda j: P(j, 0).update(file="wide.mp4"), "isn't the vertical 9:16")
    broken(lambda j: P(j, 0).update(file="short.mp4", source={"file": "source.mp4", "ranges": [[0, 3]]}), "shorter than the campaign")
    broken(lambda j: P(j, 0).update(file="nothere.mp4"), "not found")
    broken(lambda j: P(j, 2).pop("title"), "needs a title")
    broken(lambda j: P(j, 2).update(title="x" * 101), "allows 100")
    broken(lambda j: P(j, 0).update(account="Nobody"), "isn't in accounts")
    broken(lambda j: P(j, 0).update(id="Bad Id"), "short id")
    broken(lambda j: P(j, 1).update(id="p-tt"), "used twice")
    broken(lambda j: j.pop("ai_generated"), "ask the user: does the clip show")
    broken(lambda j: j["accounts"][0].update(privacyLevel="SELF_ONLY"), "can't be posted as \"only you\"")
    broken(lambda j: j["accounts"][0].pop("privacyLevel"), "who can see this tiktok")
    broken(lambda j: j["accounts"][0].pop("allowDuet"), "duets on or off")
    broken(lambda j: j["accounts"][0].update(privacyLevel="FOLLOWER_OF_CREATOR"), "campaigns pay for public views", expect=0)
    broken(lambda j: j["accounts"][0].update(socialAccountId="<from social_accounts_list>"), "placeholder")
    broken(lambda j: j["accounts"][2].pop("madeForKids"), "made for kids")
    broken(lambda j: j["accounts"][2].update(privacy="unlisted"), "earn nothing", expect=0)
    # a second TikTok account with mostly the same footage: blocked; another platform with its own hook
    # and caption: a warning only; the same caption: blocked
    second = {"name": "TikTok 2", "platform": "TIKTOK", "handle": "@example.four", "socialAccountId": "example-tt-2",
              "privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": True, "allowDuet": False, "allowStitch": False}
    plain_clip(d, "a2.mp4", 1, 8)

    def two_tiktoks(j):
        j["accounts"].append(second)
        j["posts"].append(declared("p-tt2", "TikTok 2", "a2.mp4", [[1, 9]], caption=caption("another take")))
    broken(two_tiktoks, "both TikTok and share")

    def other_platform(j, same_caption):
        j["posts"][1].update(file="a2.mp4", source={"file": "source.mp4", "ranges": [[1, 9]]}, hook="Other hook",
                             caption=caption() if same_caption else "#ad Something else entirely about it. "
                             "@example.brand example.com/test #testcard")
        j["posts"][0]["hook"] = "First hook"
    broken(lambda j: other_platform(j, True), "same hook or caption")
    broken(lambda j: other_platform(j, False), "isn't blocked", expect=0)
    broken(lambda j: j["accounts"].append({"name": "X", "platform": "X"}), "platform must be one of")
    wjson(d, "campaign-noyt.json", campaign(platforms={"TIKTOK": {"rate_per_1k": 1.0}, "INSTAGRAM": {"rate_per_1k": 0.8}}))
    broken(lambda j: j.update(campaign="campaign-noyt.json"), "doesn't pay for YouTube")
    wjson(d, "campaign-approve.json", campaign(approval_before_posting=True))
    broken(lambda j: j.update(campaign="campaign-approve.json"), "approval")
    for text in ("[]", '{"campaign": "campaign.json"}', '{"campaign": "campaign.json", "accounts": "x", "posts": []}',
                 '{"campaign": "campaign.json", "accounts": [{"name": "a", "platform": "TIKTOK"}], "posts": "x"}',
                 '{"campaign": "campaign.json", "accounts": [{"name": "a"}, {"name": "a"}], "posts": [{}]}',
                 '{"campaign": "nothere.json", "accounts": [], "posts": []}', '{broken'):
        with open(os.path.join(d, "bad-job.json"), "w", encoding="utf-8") as fh:
            fh.write(text)
        refused("check_clips.py", "bad-job.json", cwd=d)
    code, out = run("check_clips.py", "clipjob.json", "--json", cwd=d)
    ok(json.loads(out)["errors"] == 0, "--json output")
    if have_manifest:
        mj = job([{"id": "m-tt", "account": "TikTok", "file": "clips/clip-01.mp4", "caption": caption()},
                  {"id": "m-ig", "account": "Reels", "file": "clips/clip-02.mp4", "caption": caption("#reels")},
                  {"id": "m-yt", "account": "Shorts", "file": "clips/clip-03.mp4",
                   "caption": "#ad Another part of the test card. @example.brand example.com/test #testcard",
                   "title": "Third", "tags": []}])
        wjson(d, "manifest-job.json", mj)
        code, out = run("check_clips.py", "manifest-job.json", cwd=d, expect=None)
        # clip 1 (0 to 9.8 s) and clip 3 (2 to 11.8 s) overlap about 80% on two platforms with their own hooks
        ok("0 error(s)" in out and "isn't blocked" in out, f"manifest job:\n{out}")
        mj["posts"][2]["file"] = "clips/clip-01.mp4"
        wjson(d, "manifest-job.json", mj)
        code, out = run("check_clips.py", "manifest-job.json", cwd=d, expect=1)
        ok("same file" in out, "manifest job: same file twice")
    return good


def plan_tests(d, good):
    T0 = "2026-11-16T14:00:00Z"
    wjson(d, "clipjob.json", good)
    code, out = run("plan.py", "clipjob.json", cwd=d, now=T0)
    for f in ("plan.md", "plan.html", "requests/test-campaign.json", "requests/index.json", "by-hand/p-yt.md"):
        ok(os.path.isfile(os.path.join(d, f)), f"plan.py should write {f}")
    body = rjson(os.path.join(d, "requests"), "test-campaign.json")
    ok(body["schedule"] == {"type": "PUBLISH_NOW"}, "posts go out now")
    kids = {k["platform"]: k for k in body["socialAccounts"]}
    ok(set(kids) == {"TIKTOK", "INSTAGRAM"}, "connected accounts only in the request")
    tt = kids["TIKTOK"]
    ok(tt["isBrandedContent"] is True and tt["isYourBrand"] is False and tt["autoAddMusic"] is False
       and tt["privacyLevel"] == "PUBLIC_TO_EVERYONE" and tt["allowDuet"] is False, "TikTok child fields")
    ok(tt["contentOverride"][0]["media"][0]["mediaId"] == "<mediaId of a.mp4>"
       and kids["INSTAGRAM"]["contentOverride"][0]["media"][0]["mediaId"] == "<mediaId of b.mp4>", "own clip per child")
    ok(kids["INSTAGRAM"]["contentOverride"][0]["text"].startswith("#ad"), "own caption per child")
    ok("paidPartnership" not in json.dumps(body) and "brandedContent" not in json.dumps(kids["INSTAGRAM"]),
       "no invented Instagram field")
    page = open(os.path.join(d, "plan.md"), encoding="utf-8").read()
    ok("Never followed" in page and "Expected earnings" in page and "paid partnership label" in page
       and "includes paid promotion" in page and "Profile bio" in page, "plan.md content")
    ok("Pays nothing under 5,000 views" in page and "10,000 views: $9.00" in page, "earnings math on the plan")
    html = open(os.path.join(d, "plan.html"), encoding="utf-8").read()
    ok("prefers-color-scheme:dark" in html and "<title>Clip plan</title>" in html, "plan.html")
    hand = open(os.path.join(d, "by-hand", "p-yt.md"), encoding="utf-8").read()
    ok("paid promotion" in hand and "30 minutes" in hand and "paste it back to your agent" in hand
       and "python" not in hand.lower() and hand.index("## Before you post") < hand.index("## Post it"), "by-hand page")
    # errors: nothing old left that could still be sent
    broken = copy.deepcopy(good)
    broken["posts"][0]["caption"] = "no disclosure here"
    wjson(d, "clipjob.json", broken)
    run("plan.py", "clipjob.json", cwd=d, expect=1, now=T0)
    ok(not os.path.exists(os.path.join(d, "requests")), "no request survives a plan with errors")
    ok(os.path.isfile(os.path.join(d, "plan.md")), "the plan still shows the errors")
    wjson(d, "clipjob.json", good)
    run("plan.py", "clipjob.json", cwd=d, now=T0)
    # media ids
    wjson(d, "media.json", media(d, a_mp4="example-media-a", b_mp4="example-media-b"))
    run("plan.py", "clipjob.json", "--media", "media.json", cwd=d, now=T0)
    body = rjson(os.path.join(d, "requests"), "test-campaign.json")
    ok("example-media-a" in json.dumps(body) and "<mediaId" not in json.dumps(body), "--media fills the ids")
    wjson(d, "media.json", {"zzz.mp4": {"mediaId": "x", "sha256": "0" * 64}})
    refused("plan.py", "clipjob.json", "--media", "media.json", cwd=d, needle="isn't a file of this plan")
    wjson(d, "media.json", {"a.mp4": "example-media-a", "b.mp4": "example-media-b"})
    refused("plan.py", "clipjob.json", "--media", "media.json", cwd=d, needle="no fingerprint")
    wjson(d, "media.json", media(d, a_mp4="example-media-a", b_mp4="example-media-b"))
    run("plan.py", "clipjob.json", "--media", "media.json", cwd=d, now=T0)
    # posted, status, sheet
    refused("plan.py", "clipjob.json", "--posted", "requests/nope.json", "example-post-1", cwd=d, needle="isn't one of")
    refused("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", " ", cwd=d, needle="post id")
    code, out = run("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", "example-post-1", cwd=d, now=T0)
    ok("not confirmed live yet" in out and "To be safe, submit by" in out and "min left" not in out,
       f"the sheet before the posts are confirmed live has no countdown:\n{out}")
    led = rjson(HOME[0], "ledger.json")
    ok(len(led["posts"]) == 2 and all(len(e["sha256"]) == 64 and e["campaign"] == "test-campaign" for e in led["posts"].values()),
       "ledger records both posts with fingerprints")
    refused("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", "example-post-1", cwd=d, needle="already recorded")
    status = {"id": "example-post-1", "socialAccountPosts": [
        {"socialAccountId": "example-tt-1", "deliveryStatus": "PUBLISHED",
         "externalPostUrl": "https://www.tiktok.com/@example.one/video/example-1", "deliveryCompletedAt": "2026-11-16T14:01:00Z"},
        {"socialAccountId": "example-ig-1", "deliveryStatus": "PROCESSING"}]}
    wjson(d, "status.json", status)
    code, out = run("plan.py", "clipjob.json", "--status", "status.json", cwd=d, now="2026-11-16T14:05:00Z")
    ok("https://www.tiktok.com/@example.one/video/example-1" in out, "the link is on the sheet")
    ok("Mon 16 Nov 15:31 Europe/Amsterdam" in out and "26 min left" in out, f"deadline from deliveryCompletedAt:\n{out}")
    ok("not there yet" in out, "a post without a link says so")
    sheet = open(os.path.join(d, "submit.md"), encoding="utf-8").read()
    ok("paid partnership label" in sheet and "tell your agent it is submitted" in sheet, "submit.md has the app steps")
    ok("## Proof to keep" in sheet and "48h" in sheet and "72h" in sheet and "7d" in sheet
       and "proof/p-tt/" in sheet, f"submit.md has the screenshot schedule and evidence folders:\n{sheet[-900:]}")
    ok(os.path.isdir(os.path.join(d, "proof", "p-tt")), "a per-post evidence folder is made")
    wjson(d, "status.json", {"nothing": True})
    refused("plan.py", "clipjob.json", "--status", "status.json", cwd=d, needle="no per-account posts")
    # planning again leaves the recorded posts out
    code, out = run("plan.py", "clipjob.json", cwd=d, now=T0)
    ok(not os.path.exists(os.path.join(d, "requests", "test-campaign.json")), "posted accounts are left out")
    ok("already posted" in open(os.path.join(d, "plan.md"), encoding="utf-8").read(), "plan shows already posted")
    # failed: a post with a link is never released; one without is
    refused("plan.py", "clipjob.json", "--failed", "p-tt", cwd=d, needle="never released")
    refused("plan.py", "clipjob.json", "--failed", "p-ig", cwd=d, needle="still processing")
    wjson(d, "status.json", {"id": "example-post-1", "socialAccountPosts": [
        {"socialAccountId": "example-ig-1", "deliveryStatus": "FAILED", "errorMessage": "media rejected"}]})
    run("plan.py", "clipjob.json", "--status", "status.json", cwd=d, now="2026-11-16T14:06:00Z")
    refused("plan.py", "clipjob.json", "--failed", "p-ig", cwd=d, needle="social_account_posts_list")
    code, out = run("plan.py", "clipjob.json", "--failed", "p-ig", "--checked", cwd=d)
    run("plan.py", "clipjob.json", cwd=d, now=T0)
    body = rjson(os.path.join(d, "requests"), "test-campaign.json")
    ok([k["platform"] for k in body["socialAccounts"]] == ["INSTAGRAM"], "the released post goes out again, alone")
    refused("plan.py", "clipjob.json", "--failed", "nothing", cwd=d, needle="no post")
    run("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", "example-post-2", cwd=d, now=T0)
    wjson(d, "status.json", {"id": "example-post-2", "socialAccountPosts": [
        {"socialAccountId": "example-ig-1", "deliveryStatus": "PUBLISHED",
         "externalPostUrl": "https://www.instagram.com/reel/example-2/", "deliveryCompletedAt": "2026-11-16T14:20:00Z"}]})
    run("plan.py", "clipjob.json", "--status", "status.json", cwd=d, now="2026-11-16T14:21:00Z")
    # by hand
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "not-a-link", cwd=d, needle="link")
    refused("plan.py", "clipjob.json", "--posted-by-hand", "nobody", "https://youtube.com/shorts/x", cwd=d, needle="no post")
    code, out = run("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://www.youtube.com/shorts/example-3",
                    "--at", "2026-11-16T14:10:00Z", cwd=d, now="2026-11-16T14:12:00Z")
    ok("Mon 16 Nov 15:40 Europe/Amsterdam" in out, f"by-hand deadline:\n{out}")
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://www.youtube.com/shorts/example-3", cwd=d,
            needle="already")
    code, out = run("plan.py", "clipjob.json", cwd=d, now=T0)
    ok("nothing left to post" in out, "everything recorded")
    # the same edit in another campaign is refused
    other = os.path.join(d, "second")
    os.makedirs(other)
    for f in ("a.mp4", "b.mp4", "c.mp4", "brief.txt"):   # no ledger copied: the user-level one is shared
        shutil.copy(os.path.join(d, f), os.path.join(other, f))
    wjson(other, "campaign.json", campaign(id="second-campaign"))
    wjson(other, "clipjob.json", good)
    code, out = run("check_clips.py", "clipjob.json", cwd=other, expect=1)
    ok("already posted for campaign test-campaign" in out, f"edit reused across campaigns:\n{out[-800:]}")


def ledger_tests(d):
    T = "2026-11-16T14:15:00Z"
    tt, ig, yt = ("https://www.tiktok.com/@example.one/video/example-1", "https://www.instagram.com/reel/example-2/",
                  "https://www.youtube.com/shorts/example-3")
    code, out = run("ledger.py", "--submitted", tt, "--campaign", "test-campaign", cwd=d, now=T)
    ok("recorded" in out and "warning" not in out, "a timely submission")
    refused("ledger.py", "--submitted", tt, "--campaign", "test-campaign", cwd=d, needle="already submitted")
    refused("ledger.py", "--submitted", "https://tiktok.com/@example.one/video/example-1/", "--campaign", "other-campaign",
            cwd=d, needle="one campaign, once")
    refused("ledger.py", "--submitted", ig, "--campaign", "other-campaign", cwd=d, needle="made for campaign test-campaign")
    refused("ledger.py", "--submitted", ig, "--campaign", "test-campaign", "--at", "2026-11-16T14:00:00Z", cwd=d,
            needle="before", now="2026-11-16T15:30:00Z")
    code, out = run("ledger.py", "--submitted", ig, "--campaign", "test-campaign", cwd=d, now="2026-11-16T15:30:00Z")
    ok("warning: submitted 70 minutes" in out, f"a late submission is recorded with a warning:\n{out}")
    run("ledger.py", "--submitted", yt, "--campaign", "test-campaign", cwd=d, now=T)
    code, out = run("ledger.py", "--submitted", "https://www.tiktok.com/@someone/video/elsewhere", "--campaign", "test-campaign",
                    cwd=d, now=T)
    ok("isn't a post the skill recorded" in out, "an outside post is recorded with a note")
    refused("ledger.py", "--submitted", "not-a-link", "--campaign", "test-campaign", cwd=d, needle="link")
    refused("ledger.py", "--submitted", tt, cwd=d, needle="--campaign")
    refused("ledger.py", "--submitted", "https://x.test/1", "--campaign", "Bad Id", cwd=d, needle="campaign id")
    refused("ledger.py", cwd=d, needle="say what to do")
    analytics = {"posts": [
        {"externalPostUrl": tt, "metricsCollectedAt": "2026-11-19T06:00:00Z", "metrics": {"views": 7420, "likes": 248, "comments": 17, "shares": 21}},
        {"externalPostUrl": ig, "metricsCollectedAt": "2026-11-19T06:00:00Z", "metrics": {"views": 2380, "likes": 41, "comments": 2, "shares": 4}},
        {"externalPostUrl": yt, "metricsCollectedAt": "2026-11-19T06:00:00Z", "metrics": {"views": 400000, "likes": 30000, "comments": 2, "shares": 0}},
        {"externalPostUrl": "https://www.tiktok.com/@nobody/video/1", "metrics": {"views": 1}},
        {"externalPostUrl": "https://www.tiktok.com/@example.one/video/example-1", "metricsCollectedAt": "2026-11-18T06:00:00Z", "metrics": {}}]}
    wjson(d, "analytics.json", analytics)
    code, out = run("ledger.py", "--proof", "analytics.json", cwd=d, now="2026-11-19T08:00:00Z")
    ok("3 snapshot(s) added" in out and "1 without a view count" in out, f"proof:\n{out}")
    code, out = run("ledger.py", "--proof", "analytics.json", cwd=d, now="2026-11-19T09:00:00Z")
    ok("0 snapshot(s) added" in out, "the same collection isn't added twice")
    with open(home_file("proof.csv"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    ok(len(lines) == 4 and lines[0].startswith("collected_at,"), "proof.csv rows")
    ok(not os.path.exists(os.path.join(d, "proof.csv")), "the proof log sits next to the ledger, not in the folder")
    wjson(d, "analytics.json", {"nothing": []})
    refused("ledger.py", "--proof", "analytics.json", cwd=d, needle="holds no posts")
    code, out = run("ledger.py", "--earnings", cwd=d, now="2026-11-19T10:00:00Z")
    ok("7,420 views, est. $6.68" in out, f"earnings TikTok:\n{out}")
    ok("2,380 views, est. $0.00" in out and "under the $5.00 minimum" in out, "earnings under the minimum")
    ok("400,000 views, est. $135.00" in out and "capped" in out, "earnings capped")
    ok("engagement 2.0% is under" in out, "engagement floor")
    ok("times shown" in out and "own view count decides" in out, "the honest notes")
    ok("TikTok @example.one: 7,420 views, est. $6.68 after 10% fee, 30 day(s) left" in out
       and "window starts when it reaches 6,250 views" in out, f"the window counts from the payout threshold:\n{out}")
    ok("Not made here: https://www.tiktok.com/@someone/video/elsewhere" in out and "?:" not in out, "an outside post is named")
    ok(out.count("never with bought or arranged engagement") == 1, "advice said once")
    code, out = run("ledger.py", "--list", cwd=d)
    ok("submission(s)" in out, "--list")
    with open(home_file(), "w", encoding="utf-8") as fh:
        fh.write('{"posts": []}')
    refused("ledger.py", "--list", cwd=d, needle="damaged")
    refused("ledger.py", "--list", "--dir", "nothere", cwd=d, needle="not a folder")


def fix_tests(d):
    """The review's findings, one by one: a fresh folder and a fresh user home."""
    f = os.path.join(d, "fixes")
    os.makedirs(f)
    for name in ("a.mp4", "b.mp4", "c.mp4", "source.mp4", "brief.txt"):
        shutil.copy(os.path.join(d, name), os.path.join(f, name))
    HOME[0] = os.path.join(d, "home-2")
    T0 = "2026-11-16T14:00:00Z"
    posts = [declared("p-tt", "TikTok", "a.mp4", [[0, 8]]), declared("p-ig", "Reels", "b.mp4", [[20, 28]]),
             declared("p-yt", "Shorts", "c.mp4", [[30, 38]])]
    good = job(posts)

    def check(camp_over=None, job_change=None, expect=None, now=T0):
        wjson(f, "campaign.json", campaign(**(camp_over or {})))
        j = copy.deepcopy(good)
        if job_change:
            job_change(j)
        wjson(f, "clipjob.json", j)
        return run("check_clips.py", "clipjob.json", cwd=f, expect=expect, now=now)[1]

    # a brief that forbids the disclosure on screen: a conflict the skill won't follow
    with open(os.path.join(f, "conflict.txt"), "w", encoding="utf-8") as fh:
        fh.write("No paid partnership text on the video. No engagement groups or bought views. Never use a VPN.\n")
    wjson(f, "campaign.json", campaign(brief_file="conflict.txt"))
    code, out = run("campaign.py", "campaign.json", cwd=f, expect=None)
    ok("conflict" in out.lower() and "no paid partnership text" in out.lower(), f"disclosure conflict flagged:\n{out[-900:]}")
    ok("Never done" not in out, f"the brief's own ban on engagement groups was flagged:\n{out[-900:]}")
    ok("skip this campaign" in open(os.path.join(f, "checklist.md"), encoding="utf-8").read().lower(),
       "the checklist says to ask the brand or skip")
    # fields left null when the brief is silent: defaults, no crash, no "None days"
    nulls = dict(fee_pct=None, window_days=None, days_live=None, submit_within_minutes=None, max_overlap=None,
                 min_payout=None, cap_per_clip=None, budget_total=None, audience=None, proof=None,
                 required_sound=None, banned=None, engagement_min=None, bio=None, links_where=None, window_from=None)
    wjson(f, "campaign.json", campaign(**nulls))
    code, out = run("campaign.py", "campaign.json", cwd=f, expect=0)
    ok("None" not in out and "30 days" in out, f"null fields read as defaults:\n{out[-900:]}")
    wjson(f, "clipjob.json", good)
    run("check_clips.py", "clipjob.json", cwd=f, expect=0)
    code, out = run("plan.py", "clipjob.json", cwd=f, expect=0, now=T0)
    ok("None" not in open(os.path.join(f, "plan.md"), encoding="utf-8").read(), "no None on the plan")
    wjson(f, "campaign.json", campaign(length=None, footage=None))
    out = refused("campaign.py", "campaign.json", cwd=f, needle="footage")
    ok("None" not in out, "a missing field gets a message, not None")
    # per-platform minimum and cap, a zero cap, the remaining budget
    wjson(f, "campaign.json", campaign(platforms={"TIKTOK": {"rate_per_1k": 1.0, "min_payout": 20, "cap_per_clip": 300},
                                                  "INSTAGRAM": {"rate_per_1k": 0.8}, "YOUTUBE": {"rate_per_1k": 0.5}}))
    run("campaign.py", "campaign.json", cwd=f, expect=0)
    out = open(os.path.join(f, "checklist.md"), encoding="utf-8").read()
    ok("20,000" in out and "$300.00" in out, f"the TikTok minimum and cap are its own:\n{out[-1500:]}")
    wjson(f, "campaign.json", campaign(cap_per_clip=0))
    refused("campaign.py", "campaign.json", cwd=f, needle="cap")
    out = check(dict(budget_remaining=0), expect=1)
    ok("remaining budget is 0" in out, "an empty budget blocks")
    out = check(dict(budget_remaining=500, budget_checked_at="2026-11-10T09:00:00Z"), expect=0)
    ok("only $500.00 of the budget is left" in out and "read 6 day(s) ago" in out, f"budget warnings:\n{out[-900:]}")
    # accounts verified in the marketplace; their audience
    out = check(job_change=lambda j: j["accounts"][0].update(marketplace_verified=False), expect=1)
    ok("not verified in test" in out, "an unverified account blocks")
    out = check(job_change=lambda j: j["accounts"][0].pop("marketplace_verified"), expect=0)
    ok("connected and verified in test?" in out, "an unknown account asks")
    out = check(job_change=lambda j: j["accounts"][0].update(audience={"US": 0.12}), expect=0)
    ok("audience is about 12% US" in out, "a known audience under the rule warns")
    # captions: #ad_ isn't #ad; a longer link isn't the link; YouTube refuses < and >
    out = check(job_change=lambda j: j["posts"][0].update(caption=caption().replace("#ad ", "#ad_campaign ")), expect=1)
    ok("must start with #ad" in out, "#ad_campaign doesn't count as #ad")
    out = check(job_change=lambda j: j["posts"][0].update(caption=caption().replace("example.com/test", "example.com/testing")),
                expect=1)
    ok("requires the link" in out, "a longer link doesn't count")
    out = check(job_change=lambda j: j["posts"][2].update(title="Less <than> you think"), expect=1)
    ok("refuses < and >" in out, "YouTube < and >")
    # links in the bio only: the caption may leave them out, the bio must be set
    out = check(dict(links_where="bio"), job_change=lambda j: j["posts"][0].update(
        caption=caption().replace("example.com/test", "")), expect=0)
    wjson(f, "campaign.json", campaign(links_where="bio", bio=None))
    refused("campaign.py", "campaign.json", cwd=f, needle="bio")
    # footage recency: the recording day, never the file date
    out = check(dict(footage={"allowed": "supplied only", "files": ["source.mp4"], "recency_days": 7}), expect=1)
    ok("which day" in out and "footage.dates" in out, "an unknown recording day asks for it")
    out = check(dict(footage={"allowed": "supplied only", "files": ["source.mp4"], "recency_days": 7, "dates": {"source.mp4": "2026-10-01"}}), expect=1)
    ok("46 days ago" in out, f"old footage blocks:\n{out[-700:]}")
    check(dict(footage={"allowed": "supplied only", "files": ["source.mp4"], "recency_days": 7, "dates": {"Source.MP4": "2026-11-14"}}), expect=0)
    # the brand's approval: a packet to send; a fingerprint that must match
    wjson(f, "campaign.json", campaign(approval_before_posting=True))
    wjson(f, "clipjob.json", good)
    run("plan.py", "clipjob.json", cwd=f, expect=1, now=T0)
    ok(os.path.isfile(os.path.join(f, "approval", "p-tt.md")), "plan.py writes the approval packet")
    packet = open(os.path.join(f, "approval", "p-tt.md"), encoding="utf-8").read()
    fp = re.search(r"\b([0-9a-f]{12,64})\b", packet)
    ok(fp is not None and "a.mp4" in packet and caption()[:20] in packet, "the packet has the file, caption and fingerprint")
    j = copy.deepcopy(good)
    for p_ in j["posts"]:
        p_["approved"] = {"by": "brand", "at": "2026-11-15", "sha256": "0" * 12}
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("changed after the brand approved" in out, "a changed file is caught")
    # own campaign: own content on own accounts, no paid partnership
    own = campaign(own_content=True, caption={"disclosure": "", "hashtags": [], "mentions": [], "links": []},
                   on_screen={"disclosure": "", "watermark": None}, bio=None, brief_file=None, brand=None,
                   marketplace=None, min_payout=None, cap_per_clip=None, budget_total=None, fee_pct=None,
                   platforms={"TIKTOK": {"rate_per_1k": 0}, "INSTAGRAM": {"rate_per_1k": 0}, "YOUTUBE": {"rate_per_1k": 0}},
                   promotes_own_business=False)
    # own_content never switches off the disclosure of a paid campaign
    for over, needle in ((dict(marketplace="Whop"), "marketplace"), (dict(brand="Example Brand"), "brand"),
                         (dict(platforms={"TIKTOK": {"rate_per_1k": 1.0}}), "rate per 1,000"),
                         (dict(min_payout=5), "minimum payout"), (dict(cap_per_clip=100), "cap per clip"),
                         (dict(budget_total=1000), "budget"), (dict(fee_pct=10), "fee"),
                         (dict(platforms={"TIKTOK": {"rate_per_1k": 0, "min_payout": 5}}), "minimum payout"),
                         (dict(promotes_own_business=None), "promote your own product")):
        wjson(f, "campaign.json", dict(own, **over))
        refused("campaign.py", "campaign.json", cwd=f, needle=needle)
    wjson(f, "campaign.json", campaign(own_content=True, caption={"disclosure": "", "hashtags": [], "mentions": [], "links": []}))
    out = refused("campaign.py", "campaign.json", cwd=f, needle="own_content is for your own content")
    ok("Never followed" in out and "don't make it feel like an ad" in out.lower(),
       f"hide-the-ad sentences are shown with own_content too:\n{out[-1200:]}")
    j = copy.deepcopy(good)
    for p_ in j["posts"]:
        p_["caption"] = "A test card for the checks."
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("own_content is for your own content" in out, "check_clips blocks own_content on a paid campaign")
    run("plan.py", "clipjob.json", cwd=f, expect=1, now=T0)
    ok(not os.path.exists(os.path.join(f, "requests")), "no request for own_content on a paid campaign")
    with open(os.path.join(f, "own-brief.txt"), "w", encoding="utf-8") as fh:
        fh.write("My own podcast clips. Don't make it feel like an ad.\n")
    wjson(f, "campaign.json", dict(own, brief_file="own-brief.txt"))
    refused("campaign.py", "campaign.json", cwd=f, needle="paid campaign")
    wjson(f, "campaign.json", own)
    oj = copy.deepcopy(good)
    for p_ in oj["posts"]:
        p_["caption"] = "A test card for the checks."
        p_["on_screen"] = {"disclosure": None}
    wjson(f, "clipjob.json", oj)
    run("campaign.py", "campaign.json", cwd=f, expect=0)
    run("check_clips.py", "clipjob.json", cwd=f, expect=0)
    run("plan.py", "clipjob.json", cwd=f, expect=0, now=T0)
    body = rjson(os.path.join(f, "requests"), "test-campaign.json")
    tt = [k for k in body["socialAccounts"] if k["platform"] == "TIKTOK"][0]
    ok(tt["isYourBrand"] is False and tt["isBrandedContent"] is False and tt["autoAddMusic"] is False,
       "own show: no Your brand label, not branded content")
    page = open(os.path.join(f, "plan.md"), encoding="utf-8").read()
    ok("Paid partnership" not in page and "paid promotion" not in page and "Your brand" not in page,
       "own campaign: no paid partnership steps")
    ok(not any("category" in k for k in body["socialAccounts"]), "no YouTube category sent")
    wjson(f, "campaign.json", dict(own, promotes_own_business=True))
    run("plan.py", "clipjob.json", cwd=f, expect=0, now=T0)
    body = rjson(os.path.join(f, "requests"), "test-campaign.json")
    tt = [k for k in body["socialAccounts"] if k["platform"] == "TIKTOK"][0]
    ok(tt["isYourBrand"] is True and tt["isBrandedContent"] is False, "own content promoting own business: Your brand")
    # a required TikTok sound: by hand, added before posting
    wjson(f, "campaign.json", campaign(required_sound={"name": "Test Song", "platforms": ["TIKTOK"], "volume": "10%"}))
    wjson(f, "clipjob.json", good)
    run("plan.py", "clipjob.json", cwd=f, expect=0, now=T0)
    body = rjson(os.path.join(f, "requests"), "test-campaign.json")
    ok([k["platform"] for k in body["socialAccounts"]] == ["INSTAGRAM"], "the TikTok post needing a sound isn't sent")
    hand = open(os.path.join(f, "by-hand", "p-tt.md"), encoding="utf-8").read()
    ok("Test Song" in hand and "before posting" in hand and "after posting" not in hand.replace("can't be added after posting", ""),
       "the sound is added in the app before posting")
    # saved accounts, across campaigns
    wjson(f, "campaign.json", campaign())
    run("plan.py", "clipjob.json", "--remember-accounts", cwd=f, expect=0, now=T0)
    saved = rjson(HOME[0], "accounts.json")
    ok(len(saved.get("accounts", saved)) == 3, "accounts saved for the next campaign")
    saved = rjson(HOME[0], "accounts.json")["accounts"]
    ok(not any(k in a for a in saved for k in ("socialAccountId", "privacyLevel", "verified_in", "marketplace_verified")),
       f"no id, TikTok privacy or verification is saved: {saved}")
    # the same name with another handle: nothing carried over, the request names this account's id only
    j = copy.deepcopy(good)
    j["accounts"][0] = {"name": "TikTok", "platform": "TIKTOK", "handle": "@second.tiktok", "socialAccountId": "example-tt-9",
                        "marketplace_verified": True}
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("who can see this tiktok" in out.lower() and "saved choice" not in out.lower(),
       f"another handle under a saved name gets nothing from the saved file:\n{out[-900:]}")
    # the same platform and handle: suggestions only, never filled in
    j = copy.deepcopy(good)
    j["accounts"][0] = {"name": "Main TikTok", "platform": "TIKTOK", "handle": "@Example.One", "socialAccountId": "example-tt-1",
                        "privacyLevel": "PUBLIC_TO_EVERYONE", "marketplace_verified": True}
    j["posts"][0]["account"] = "Main TikTok"
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("saved choice for Main TikTok" in out and "duets off" in out and "duets on or off" in out,
       f"saved choices are suggestions, not filled in:\n{out[-900:]}")
    # no socialAccountId: by hand only when said so; both is a contradiction
    j = copy.deepcopy(good)
    j["accounts"][2].pop("by_hand")
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("say how this account posts" in out, "an account without id or by_hand asks")
    j["accounts"][2].update(by_hand=True, socialAccountId="example-yt-1")
    wjson(f, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=f, expect=1)[1]
    ok("by_hand is true and the account also has a socialAccountId" in out, "by_hand and an id together")
    # posting: the ledger is the user's, the clock is the user's
    wjson(f, "clipjob.json", good)
    wjson(f, "media.json", media(f, a_mp4="m-a"))
    refused("plan.py", "clipjob.json", "--media", "media.json", cwd=f, needle="no mediaId for b.mp4", now=T0)
    mm = media(f, a_mp4="m-a", b_mp4="m-b")
    mm["b.mp4"]["mediaId"] = ""
    wjson(f, "media.json", mm)
    refused("plan.py", "clipjob.json", "--media", "media.json", cwd=f, needle="mediaId", now=T0)
    wjson(f, "media.json", media(f, a_mp4="m-a", b_mp4="m-b"))
    run("plan.py", "clipjob.json", "--media", "media.json", cwd=f, now=T0)
    run("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", "post-9", cwd=f, now=T0)
    ok(os.path.isfile(home_file()), "the ledger lives in the user's folder")
    ok(not os.path.exists(os.path.join(f, "ledger.json")), "no ledger in the campaign folder")
    # --status: matched by post id, never by account alone; wrapped replies; wrong ids refused
    st = {"result": {"post": {"id": "post-9", "socialAccountPosts": [
        {"socialAccountId": "example-tt-1", "deliveryStatus": "PUBLISHED",
         "externalPostUrl": "https://www.tiktok.com/@example.one/video/7300000000000000001?is_from_webapp=1",
         "deliveryCompletedAt": "2026-11-16T14:01:00Z"},
        {"socialAccountId": "example-ig-1", "deliveryStatus": "PROCESSING"}]}}}
    wjson(f, "status.json", st)
    refused("plan.py", "clipjob.json", "--status", "status.json", "post-other", cwd=f, needle="post-other",
            now="2026-11-16T14:05:00Z")
    code, out = run("plan.py", "clipjob.json", "--status", "status.json", cwd=f, now="2026-11-16T14:05:00Z")
    ok("26 min left" in out and "(CET)" in out, f"wrapped reply, zone shown:\n{out[-800:]}")
    ok(out.count("min left") == 1 and "not confirmed live yet" in out, f"a processing post has no countdown:\n{out}")
    bad = copy.deepcopy(st)
    bad["result"]["post"].pop("id")
    wjson(f, "status.json", bad)
    refused("plan.py", "clipjob.json", "--status", "status.json", cwd=f, needle="post id")
    # submitting: tracking strings don't make another post; share links are refused
    code, out = run("ledger.py", "--submitted", "https://tiktok.com/@example.one/video/7300000000000000001?_r=1&_t=x",
                    "--campaign", "test-campaign", "--submission-id", "sub-77", cwd=f, now="2026-11-16T14:10:00Z")
    ok("recorded" in out and "isn't a post the skill recorded" not in out, f"tracking strings stripped:\n{out}")
    refused("ledger.py", "--submitted", "https://vm.tiktok.com/ZMabc/", "--campaign", "test-campaign", cwd=f, needle="share link")
    led = json.load(open(home_file(), encoding="utf-8"))
    ok(any(sx.get("submission_id") == "sub-77" for sx in led["submissions"]), "the submission id is kept")
    # another folder, another campaign: the same file is caught through the one user ledger
    g = os.path.join(d, "fixes-elsewhere")
    os.makedirs(g)
    for name in ("a.mp4", "b.mp4", "c.mp4", "source.mp4", "brief.txt"):
        shutil.copy(os.path.join(f, name), os.path.join(g, name))
    wjson(g, "campaign.json", campaign(id="elsewhere"))
    wjson(g, "clipjob.json", good)
    out = run("check_clips.py", "clipjob.json", cwd=g, expect=1)[1]
    ok("already posted for campaign test-campaign" in out, "reuse caught across folders")
    out = run("check_clips.py", "clipjob.json", "--ledger", os.path.join(g, "empty-ledger.json"), cwd=g, expect=0)[1]
    ok("already posted" not in out, "--ledger points at another ledger")
    # by hand: a time without an offset is the job's zone; a date alone and the future are refused
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://www.youtube.com/shorts/Zz1", "--at",
            "2026-11-16", cwd=f, needle="date alone", now="2026-11-16T14:12:00Z")
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://www.youtube.com/shorts/Zz1", "--at",
            "2026-11-16T18:00Z", cwd=f, needle="future", now="2026-11-16T14:12:00Z")
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://vm.tiktok.com/ZMabc/", cwd=f, needle="share link")
    code, out = run("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://youtu.be/AbCdEf12345?si=x",
                    "--at", "2026-11-16T15:10", cwd=f, now="2026-11-16T14:12:00Z")
    ok("Europe/Amsterdam" in out and "15:40" in out, f"a time without an offset is the job's zone:\n{out[-600:]}")
    # a plan.md that plan.py didn't write is never deleted
    j2 = os.path.join(d, "fixes-foreign")
    shutil.copytree(f, j2, ignore=shutil.ignore_patterns("plan.md", "plan.html", "requests", "by-hand", "approval"))
    with open(os.path.join(j2, "plan.md"), "w", encoding="utf-8") as fh:
        fh.write("# my own plan\n")
    refused("plan.py", "clipjob.json", cwd=j2, needle="plan.md", now=T0)
    ok(open(os.path.join(j2, "plan.md"), encoding="utf-8").read() == "# my own plan\n", "the user's plan.md is kept")
    # the proof log as Excel saves it (a BOM), with the new columns
    with open(home_file("proof.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("collected_at,metrics_collected_at,campaign,post,account,platform,url,views,likes,comments,shares,"
                 "engagement,published_at\n2026-11-17T06:00:00Z,2026-11-17T06:00:00Z,test-campaign,p-tt,TikTok,TIKTOK,"
                 "https://www.tiktok.com/@example.one/video/7300000000000000001,6000,300,10,10,0.05,2026-11-16T14:01:00Z\n")
    code, out = run("ledger.py", "--earnings", cwd=f, now="2026-11-17T08:00:00Z")
    ok("6,000 views" in out, f"proof.csv with a BOM:\n{out[-700:]}")
    wjson(f, "analytics.json", {"posts": [{"externalPostUrl": "https://www.tiktok.com/@example.one/video/7300000000000000001",
                                           "metricsCollectedAt": "2026-11-18T06:00:00Z", "metrics": {"views": 9000}}]})
    run("ledger.py", "--proof", "analytics.json", cwd=f, now="2026-11-18T08:00:00Z")
    head = open(home_file("proof.csv"), encoding="utf-8-sig").readline()
    ok("submission_id" in head and "us_share" in head, f"proof.csv gains the new columns: {head}")
    # a window that counts from posting
    wjson(f, "campaign.json", campaign(window_from="post"))
    code, out = run("ledger.py", "--earnings", "--campaign-file", "campaign.json", cwd=f, now="2026-11-19T14:00:00Z")
    ok("27 day(s) left" in out, f"a window from posting:\n{out[-700:]}")
    HOME[0] = os.path.join(d, "user-home")

def round2_tests(d, have_manifest):
    """Round-2 review findings: a fresh folder and a fresh user home."""
    r = os.path.join(d, "round2")
    os.makedirs(r)
    for name in ("a.mp4", "b.mp4", "c.mp4", "source.mp4", "brief.txt"):
        shutil.copy(os.path.join(d, name), os.path.join(r, name))
    HOME[0] = os.path.join(d, "home-3")
    T0 = "2026-11-16T14:00:00Z"
    posts = [declared("p-tt", "TikTok", "a.mp4", [[0, 8]]), declared("p-ig", "Reels", "b.mp4", [[20, 28]]),
             declared("p-yt", "Shorts", "c.mp4", [[30, 38]])]
    good = job(posts)
    wjson(r, "campaign.json", campaign())
    # P1-3: the on-screen disclosure names this campaign's brand; a clip cut for another campaign is refused
    j = copy.deepcopy(good)
    j["posts"][0]["on_screen"] = {"disclosure": "Paid partnership with Other Brand", "disclosure_seconds": 3, "watermark": "TEST CARD"}
    wjson(r, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=r, expect=1)[1]
    ok("names another brand" in out, f"another brand on screen is refused:\n{out[-700:]}")
    j["posts"][0]["on_screen"]["disclosure"] = "paid partnership with example brand"
    wjson(r, "clipjob.json", j)
    run("check_clips.py", "clipjob.json", cwd=r, expect=0)
    if have_manifest:
        wjson(d, "campaign-other.json", campaign(id="other-brand-camp", brand="Other Brand", on_screen={
            "disclosure": "Paid partnership with Other Brand", "disclosure_seconds": 3, "watermark": None}))
        mj = job([{"id": "o-tt", "account": "TikTok", "file": "clips/clip-01.mp4", "caption": caption()}],
                 campaign="campaign-other.json")
        mj["accounts"] = mj["accounts"][:1]
        wjson(d, "other-job.json", mj)
        out = run("check_clips.py", "other-job.json", cwd=d, expect=1)[1]
        ok("cut for campaign test-campaign" in out and "names another brand" in out,
           f"a clip cut for another campaign is refused:\n{out[-900:]}")
    # P1-4: a clip cut again after its upload is refused
    wjson(r, "clipjob.json", good)
    run("plan.py", "clipjob.json", cwd=r, now=T0)
    page = open(os.path.join(r, "plan.md"), encoding="utf-8").read()
    ok("fingerprint `" in page, "the plan lists each upload's fingerprint")
    wjson(r, "media.json", media(r, a_mp4="m-a", b_mp4="m-b"))
    run("plan.py", "clipjob.json", "--media", "media.json", cwd=r, now=T0)
    plain_clip(r, "a.mp4", 1, 8)
    refused("plan.py", "clipjob.json", "--media", "media.json", cwd=r, needle="changed after it was uploaded", now=T0)
    wjson(r, "media.json", media(r, a_mp4="m-a2", b_mp4="m-b"))
    run("plan.py", "clipjob.json", "--media", "media.json", cwd=r, now=T0)
    run("plan.py", "clipjob.json", "--posted", "requests/test-campaign.json", "post-r2", cwd=r, now=T0)
    # P2-12: a link submitted before its post's link was read is refused
    refused("ledger.py", "--submitted", "https://www.tiktok.com/@example.one/video/7311111111111111111", "--campaign",
            "another-campaign", cwd=r, needle="hasn't read its link", now="2026-11-16T14:03:00Z")
    # P2-2: the posts_get reply saved as the MCP text envelope
    st = {"id": "post-r2", "socialAccountPosts": [
        {"socialAccountId": "example-tt-1", "deliveryStatus": "PUBLISHED", "deliveryCompletedAt": "2026-11-16T14:01:00Z",
         "externalPostUrl": "https://www.tiktok.com/@example.one/video/7311111111111111111"},
        {"socialAccountId": "example-ig-1", "deliveryStatus": "PUBLISHED", "deliveryCompletedAt": "2026-11-16T14:01:00Z",
         "externalPostUrl": "https://www.instagram.com/reel/R2abc/"}]}
    wjson(r, "status.json", {"content": [{"type": "text", "text": json.dumps(st)}]})
    code, out = run("plan.py", "clipjob.json", "--status", "status.json", cwd=r, now="2026-11-16T14:05:00Z")
    ok("2 post(s) updated" in out, f"an MCP envelope reply is read:\n{out[-600:]}")
    # by hand: a link of another platform is refused
    refused("plan.py", "clipjob.json", "--posted-by-hand", "p-yt", "https://www.tiktok.com/@example.one/video/7322222222222222222",
            cwd=r, needle="TikTok link", now="2026-11-16T14:10:00Z")
    # P2-11: the same moment again on the same account under a new post id is a double post
    plain_clip(r, "a3.mp4", 0.5, 8)
    j = copy.deepcopy(good)
    j["posts"][0].update(id="p-tt-again", file="a3.mp4", caption=caption("again"))
    wjson(r, "clipjob.json", j)
    out = run("check_clips.py", "clipjob.json", cwd=r, expect=1)[1]
    ok("double post" in out, f"a re-cut on the same account is refused:\n{out[-800:]}")
    wjson(r, "clipjob.json", good)
    # P1-2: the proof log sits next to the ledger; earnings read the same from any folder
    run("ledger.py", "--submitted", "https://www.tiktok.com/@example.one/video/7311111111111111111", "--campaign",
        "test-campaign", cwd=r, now="2026-11-16T14:10:00Z")
    wjson(r, "analytics.json", {"content": [{"type": "text", "text": json.dumps({"posts": [
        {"externalPostUrl": "https://www.tiktok.com/@example.one/video/7311111111111111111",
         "metricsCollectedAt": "2026-11-18T06:00:00Z", "metrics": {"views": 1840, "likes": 90}}]})}]})
    code, out = run("ledger.py", "--proof", "analytics.json", cwd=r, now="2026-11-18T08:00:00Z")
    ok("1 snapshot(s) added" in out and os.path.isfile(home_file("proof.csv")), f"proof next to the ledger:\n{out}")
    g = os.path.join(d, "round2-other")
    os.makedirs(g)
    a_out = run("ledger.py", "--earnings", cwd=r, now="2026-11-18T09:00:00Z")[1]
    b_out = run("ledger.py", "--earnings", cwd=g, now="2026-11-18T09:00:00Z")[1]
    ok("1,840 views" in a_out and "1,840 views" in b_out, f"earnings from another folder see the proof:\n{b_out[-800:]}")
    ok(not os.path.exists(os.path.join(r, "proof.csv")) and not os.path.exists(os.path.join(g, "proof.csv")),
       "no proof.csv in campaign folders")
    # the example's own README keeps the example out of the user's files
    readme = open(os.path.join(ROOT, "skills", "ai-clipping-skill", "examples", "orbit-campaign", "README.md"),
                  encoding="utf-8").read()
    ok("AI_CLIPPING_HOME=" in readme, "the example README moves AI_CLIPPING_HOME too")
    ok(not os.path.exists(os.path.join(ROOT, "skills", "ai-clipping-skill", "examples", "orbit-campaign", "strip-tmp")),
       "no leftover build folder in the example")
    HOME[0] = os.path.join(d, "user-home")


def media_fix_tests(d):
    """Transcripts in odd encodings, loud moments, upload sessions, and (with Pillow) the new cuts."""
    m = os.path.join(d, "media")
    os.makedirs(m)
    # a loud burst in the middle of a quiet tone: a peak candidate; captions saved as UTF-16 by a Windows tool
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40", "-f", "lavfi", "-i",
           "sine=frequency=300:duration=40", "-filter_complex",
           "[1:a]volume='if(between(t,20,22),1.0,0.05)':eval=frame[a]", "-map", "0:v", "-map", "[a]",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "source.mp4", cwd=m)
    srt = open(os.path.join(d, "source.srt"), encoding="utf-8").read()
    with open(os.path.join(m, "source.srt"), "w", encoding="utf-16") as fh:
        fh.write(srt.replace("\n", "\r\n"))
    run("transcribe.py", "source.mp4", "--from", "source.srt", "--out", "work", cwd=m)
    t = rjson(os.path.join(m, "work"), "transcript.json")
    ok(len(t["words"]) == 120, f"UTF-16 captions read: {len(t['words'])} words")
    ok(any(19 <= p["t"] <= 23 for p in t["peaks"]), f"the loud moment is a peak: {t['peaks']}")
    ok("+" in open(os.path.join(m, "work", "peaks.txt"), encoding="utf-8").read(), "peaks.txt lists it")
    # upload sessions with a part listed twice
    wjson(m, "session.json", {"partSizeInBytes": 10, "partCount": 2, "parts": [
        {"partNumber": 1, "uploadUrl": "https://x.test/1"}, {"partNumber": 1, "uploadUrl": "https://x.test/2"}]})
    shutil.copy(os.path.join(d, "a.mp4"), os.path.join(m, "a.mp4"))
    refused("upload_parts.py", "session.json", "a.mp4", cwd=m, needle="once each")
    if NO_CUT:
        return
    wjson(m, "campaign.json", campaign(footage={"allowed": "supplied only", "files": ["source.mp4", "phone.mp4", "mute.mp4",
                                                                                         "stream.mp4"]}))
    with open(os.path.join(m, "brief.txt"), "w", encoding="utf-8") as fh:
        fh.write(BRIEF)
    base = {"video": "source.mp4", "campaign": "campaign.json",
            "clips": [{"id": "1", "start": 0.0, "end": 9.8, "hook": "A test", "reframe": "center"}]}

    def cut(spec, *extra, expect=0):
        wjson(m, "clips.json", spec)
        return run("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", "--width", "540",
                   *extra, cwd=m, expect=expect)[1]
    # ids become file names; times must be numbers
    for bad_id in ("../x", "a b", "a/b", "..", "x" * 50, "Ab"):
        spec = copy.deepcopy(base)
        spec["clips"][0]["id"] = bad_id
        wjson(m, "clips.json", spec)
        refused("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", cwd=m, needle="clip id")
    for k, v in (("start", "zero"), ("end", None), ("zoom", 9), ("focus_x", 2), ("cut", [[5]])):
        spec = copy.deepcopy(base)
        spec["clips"][0][k] = v
        wjson(m, "clips.json", spec)
        refused("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", cwd=m, needle=k)
    ok(not os.path.exists(os.path.join(d, "x.mp4")) and not os.path.exists(os.path.join(m, "x.mp4")), "nothing written outside")
    # phone footage with a rotation flag: read as portrait, cut without black bars
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12", "-f", "lavfi", "-i", "sine=duration=12",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "flat.mp4", cwd=m)
    ffmpeg("-display_rotation", "90", "-i", "flat.mp4", "-c", "copy", "phone.mp4", cwd=m)
    sys.path.insert(0, S)
    import cliplib
    info = cliplib.probe(os.path.join(m, "phone.mp4"))
    ok((info["width"], info["height"]) == (720, 1280), f"rotation read: {info['width']}x{info['height']}")
    spec = dict(base, video="phone.mp4")
    cut(spec)
    from PIL import Image, ImageStat
    ffmpeg("-ss", "6", "-i", os.path.join("clips", "clip-01.mp4"), "-frames:v", "1", "rot.png", cwd=m)
    im = Image.open(os.path.join(m, "rot.png")).convert("L")
    ok(ImageStat.Stat(im.crop((0, 380, 540, 580))).mean[0] > 30, "the rotated source fills the frame")
    # a source without sound: silence under the captions, no crash
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "mute.mp4", cwd=m)
    out = cut(dict(base, video="mute.mp4"))
    ok("no sound track" in out, "a silent source is said")
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                        os.path.join(m, "clips", "clip-01.mp4")], capture_output=True, text=True).stdout
    ok("audio" in a, "the clip still has a (silent) sound track")
    # a logo from the brief, top right, inside the safe zone
    logo = Image.new("RGBA", (400, 200), (255, 0, 0, 255))
    logo.save(os.path.join(m, "logo.png"))
    wjson(m, "campaign.json", campaign(footage={"allowed": "supplied only", "files": ["source.mp4", "stream.mp4"]},
                                      on_screen={"disclosure": "Paid partnership with Example Brand", "disclosure_seconds": 3,
                                                 "watermark": {"image": "logo.png", "position": "top-right", "opacity": 1,
                                                               "scale": 0.2}}))
    run("campaign.py", "campaign.json", cwd=m)
    cut(base)
    man = rjson(os.path.join(m, "clips"), "manifest.json")
    wm = man["clips"][0]["on_screen"]["watermark"]
    ok(isinstance(wm, dict) and wm.get("image") == "logo.png" and len(wm.get("image_sha256", "")) == 64, f"logo recorded: {wm}")
    ffmpeg("-ss", "6", "-i", os.path.join("clips", "clip-01.mp4"), "-frames:v", "1", "logo.png.check.png", cwd=m)
    im = Image.open(os.path.join(m, "logo.png.check.png")).convert("RGB")
    r_, g_, b_ = ImageStat.Stat(im.crop((380, 220, 440, 240))).mean
    ok(r_ > 150 and g_ < 90 and b_ < 90, f"the logo sits top right below the tabs: {r_:.0f},{g_:.0f},{b_:.0f}")
    os.remove(os.path.join(m, "logo.png"))
    wjson(m, "clips.json", base)
    refused("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", cwd=m, needle="logo")
    # a streamer layout: facecam on top, gameplay below
    wjson(m, "campaign.json", campaign(footage={"allowed": "supplied only", "files": ["source.mp4", "stream.mp4"]}))
    shutil.copy(os.path.join(m, "source.mp4"), os.path.join(m, "stream.mp4"))
    spec = dict(base, video="stream.mp4", clips=[dict(base["clips"][0], reframe="stack", punch=True,
                                                      facecam={"x": 960, "y": 0, "w": 320, "h": 240})])
    cut(spec)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", os.path.join(m, "clips", "clip-01.mp4")], capture_output=True, text=True)
    ok(probe.stdout.strip() == "540,960", f"stack clip size {probe.stdout.strip()}")
    bad = copy.deepcopy(spec)
    bad["clips"][0].pop("facecam")
    wjson(m, "clips.json", bad)
    refused("make_clips.py", "clips.json", "--transcript", "work/transcript.json", "--out", "clips", cwd=m, needle="facecam")
    cut(spec, "--preview")

def upload_tests(d):
    refused("upload_parts.py", cwd=d, needle="usage")
    refused("upload_parts.py", "nothere.json", "a.mp4", cwd=d, needle="not found")
    wjson(d, "session.json", {"partSizeInBytes": 10, "parts": [{"partNumber": 1, "uploadUrl": "http://x.test"}]})
    refused("upload_parts.py", "session.json", "a.mp4", cwd=d, needle="https")
    wjson(d, "session.json", {"something": "else"})
    refused("upload_parts.py", "session.json", "a.mp4", cwd=d, needle="media_uploads_create_session")
    size = os.path.getsize(os.path.join(d, "a.mp4"))
    wjson(d, "session.json", {"partSizeInBytes": size, "parts": [{"partNumber": 1, "uploadUrl": "https://x.test/1"},
                                                                {"partNumber": 2, "uploadUrl": "https://x.test/2"}]})
    refused("upload_parts.py", "session.json", "a.mp4", cwd=d, needle="needs 1")


def main():
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is needed")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("ffmpeg is needed for the tests")
    unit_tests()
    d = tempfile.mkdtemp(prefix="clip-test-")
    HOME[0] = os.path.join(d, "user-home")
    try:
        make_source(d)
        with open(os.path.join(d, "brief.txt"), "w", encoding="utf-8") as fh:
            fh.write(BRIEF)
        run("check_setup.py", cwd=d)
        campaign_tests(d)
        wjson(d, "campaign.json", campaign())
        have_manifest = cut_tests(d)
        good = check_tests(d, have_manifest)
        plan_tests(d, good)
        ledger_tests(d)
        upload_tests(d)
        fix_tests(d)
        round2_tests(d, have_manifest)
        media_fix_tests(d)
        skill = os.path.join(ROOT, "skills", "ai-clipping-skill")
        stray = [os.path.join(r, f) for r, ds, fs in os.walk(skill) for f in fs + ds if f == "__pycache__" or f.endswith(".pyc")]
        ok(not stray, f"scripts wrote into the skill folder: {stray}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{checks} checks, {len(failed)} failed" + (" (make_clips.py skipped: no Pillow)" if NO_CUT else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
