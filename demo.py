import yt_dlp

opts = {"quiet": True, "skip_download": True, "noprogress": True}
with yt_dlp.YoutubeDL(opts) as y:
    r = y.extract_info("ytsearch6:1 shot machine learning video", download=False)

for e in r["entries"]:
    print({
        "title": e.get("title"),
        "channel": e.get("channel") or e.get("uploader"),
        "views": e.get("view_count"),
        "subscribers": e.get("channel_follower_count"),
        "channel_url": e.get("channel_url"),
    })