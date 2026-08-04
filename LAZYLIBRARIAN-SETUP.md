# LazyLibrarian configuration for PrettyLazyLibWrapper

This app is a front-end for LazyLibrarian. LL does the searching, downloading and
filing; the wrapper only presents it. So a correct LL configuration matters as
much as the app itself — a mis-set LL will happily corrupt a library, and several
of the settings below exist specifically to prevent failures we hit in production.

Values shown are the working configuration as of 2026-08-04.

---

## 1. Connection

| Setting | Value | Notes |
|---|---|---|
| LL URL | `http://<ll-host>:5299` | wrapper env `LL_URL` |
| LL API key | LazyLibrarian → Settings → Interface | wrapper env `LL_API_KEY` |
| `BOOK_API` | `GoodReads` | source of author/series/book metadata |

Verify with:
```
curl "http://<ll-host>:5299/api?apikey=<key>&cmd=getVersion"
```

## 2. Directories

| Setting | Value |
|---|---|
| `DOWNLOAD_DIR` | `/downloads` |
| `AUDIO_DIR` | `/audiobooks` |
| `EBOOK_DIR` | `/books` |

Keep ebooks out of the audiobook tree. They arrive bundled with audiobook
torrents (`.epub`/`.mobi`/`.azw3`) and, left alone, accumulate silently — we
found 115 of them buried in the audiobook library while `/books` held one file.

## 3. Naming — matches Audiobookshelf's parser

Audiobookshelf's documented layout is `{Author}/{Series}/{Book}`, with the series
sequence number at the **start** of the book folder followed by `" - "`.
See https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/

```
AUDIOBOOK_DEST_FOLDER = $Author/$Series/$FmtNum $Title
AUDIOBOOK_DEST_FILE   = $Author - $Title Part $Part of $Total
EBOOK_DEST_FOLDER     = $Author/$Title
EBOOK_DEST_FILE       = $Title - $Author

FMT_SERIES  = $FmtName
FMT_SERNAME = $SerName
FMT_SERNUM  = $PadNum -
```

Produces:
```
Jamie McFarlane/Spaceship Mechanic/02 - Jump Drives and Coffee Stains
Adrian Tchaikovsky/The Hungry Gods            <- standalone, no series
```

Two non-obvious details:

- **Put the trailing space in the template, not in `FMT_SERNUM`.** LL strips
  trailing whitespace from substituted variables, so `FMT_SERNUM = "$PadNum - "`
  yields `02 -Title`. Hence `FMT_SERNUM = "$PadNum -"` plus the space in
  `AUDIOBOOK_DEST_FOLDER`.
- **Use `$Series`, not a raw `$SerName/$PadNum`.** `$Series` collapses to empty
  for standalone books; the raw form produces a folder literally named
  `- The Hungry Gods` for anything without a series number.

Preview any book's resulting paths without touching files:
```
curl "http://<ll-host>:5299/api?apikey=<key>&cmd=nameVars&id=<bookid>"
```

## 4. Matching thresholds

```
DLOAD_RATIO  = 90
NAME_RATIO   = 90
IMP_PREFLANG = English, en-GB, en-US, eng, en
```

**Known weakness, unfixed upstream.** LL matches releases with
`rapidfuzz.fuzz.token_set_ratio`, which returns **100** whenever the search
tokens are a subset of the candidate. For a series whose first book shares the
series name, *every* release ties at 100:

```
request: "Dungeon Crawler Carl"
  Dungeon Crawler Carl                       -> 100
  Dungeon Crawler Carl - Season 3 (Book 3)   -> 100
  Dungeon Crawler Carl, Vol. 4 (Spanish)     -> 100
  Dungeon Crawler Carl Books 1-8             -> 100
```

LL cannot rank these and takes whichever comes first. In production it grabbed
Season 3 for a book-1 request, then marked the request satisfied. Raising the
ratio does not help — they are all already at the maximum. Expect to intervene
manually for series like this.

## 5. Renaming — and the corruption this prevents

```
IMP_RENAME = 0     (off)
```

`IMP_RENAME` only governs the **library-scan** rename path. The **download** path
renames regardless: `postprocess.py` calls `audio_rename(rename=True)`
unconditionally whenever `AUDIOBOOK_DEST_FILE` is non-empty. Do not assume
renaming is disabled.

That matters because `audio_rename()` works on the whole **folder** — it treats
every audio file present as parts of the one book being processed.

### The critical prerequisite: one book per folder

`_process_destination()` moves **every file in `book_path`** matching the book
type. When a torrent is a single loose file, qBittorrent (with the default
content layout) saves it directly into the save path, and LL's `DownloadFolder`
becomes the *entire* download directory:

```
General:/data/complete/<book>   DownloadFolder:/data/complete
```

LL then sweeps every audiobook sitting there into one book's destination. We hit
this twice: once silently (one book's audio written under two other books' names,
undetected for two weeks), once caught immediately (three Dungeon Crawler Carl
books merged into book 1's folder, which Audiobookshelf then reported as a single
54-hour title).

**Fix — in the torrent client, not LazyLibrarian:**

```
qBittorrent → Options → Downloads → Content layout = Subfolder
API: POST /api/v2/app/setPreferences  json={"torrent_content_layout":"Subfolder"}
```

Every torrent gets its own folder, so `book_path` is always a single-book
directory and LL cannot reach a neighbour. Applies to **newly added torrents
only** — fold any already-loose files by hand first.

Health check, should always be `0`:
```
find <download>/complete -maxdepth 1 -type f \
  \( -iname '*.m4b' -o -iname '*.mp3' -o -iname '*.m4a' \) | wc -l
```

## 6. Seeding / copy behaviour

```
DESTINATION_COPY = 0     (move, not copy)
KEEP_SEEDING     = 0
```

Set qBittorrent share limits too, or completed downloads accumulate forever:
ratio `1.0`, seeding time cap, and **`max_ratio_act = 2`** (remove torrent *and*
delete content). The default action removes the torrent but leaves the files —
that alone grew 97 GB of orphans here.

## 7. Metadata precedence (Audiobookshelf)

When a series name will not stick, this is why. ABS resolves in this order:

```
1. .opf  (calibre:series)   <- HIGHEST, beats everything
2. metadata.json            <- ABS's own sidecar
3. folder path              <- {Author}/{Series}/{Book}
4. embedded audio tags      <- often absent entirely
```

LL writes `.opf` files, so a stale `calibre:series` value silently overrides both
the folder name and anything set through the ABS UI or API. Changing a series
name durably means updating **every layer that is present**, then rescanning and
re-verifying — a change that looks right in the UI can be undone by the next scan.

## 8. Verifying a working setup

```bash
# search returns results
curl "http://<ll>:5299/api?apikey=<key>&cmd=findBook&name=<title>"

# request path works (bogus id: exercises the code without adding anything)
curl "http://<ll>:5299/api?apikey=<key>&cmd=addBook&id=bogus&wait=1"   # 200 + "false", never 500

# naming preview
curl "http://<ll>:5299/api?apikey=<key>&cmd=nameVars&id=<bookid>"
```

**After `addBook`, always check `AudioStatus`.** New records are created as
`Wanted`, which makes LL download a book you already own. Set `Open` with
`AudioFile`/`AudioLibrary` when registering something already on disk.

## 9. Upstream bug to be aware of

`api.py` indexes `source` (a string) as a dict in three handlers — `_findauthor`,
`_findbook` and `_addonebook` — raising
`TypeError: string indices must be integers` on every call. `_addonebook` is what
the Request button uses, so requests fail with a bare 500 while search still
works if only the `find*` handlers are patched.

Fix is a `lazylibrarian.INFOSOURCES[source]` lookup before use. The traceback is
**not** visible in `docker logs` at INFO level — hit the API directly with curl to
see CherryPy's debug page.
