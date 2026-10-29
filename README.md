# Sort X

Sort X is a home for two tools that tidy up folders on your Mac:

| App | What it does |
|---|---|
| **.X** (Files) | Sorts every file in a folder into category folders and sets aside exact duplicate files. |
| **.Image** (Pictures) | Scans the pictures in a folder for exact duplicates (same pixels) and similar pictures (resized, re-saved or edited copies). Shows them side by side so you can keep or remove each copy. |

Everything runs on your Mac only. Nothing is uploaded, and nothing is ever permanently
deleted: anything you remove goes to the macOS Trash.

## Opening Sort X

Double-click **`Sort X.command`** in Finder (first time: right-click → Open). Or run:

```bash
python3 file_sorter.py
```

Sort X opens in its own window (a Chrome app window, or your default browser if Chrome
isn't installed) on the **home screen**. Pick **.X** or **.Image** there, or switch at any
time with the **Home / Files / Pictures** switcher at the top.

Sort X follows your Mac's light/dark setting until you press the **Dark mode / Light mode**
button at the top right. Your theme and each app's last folder and options are remembered in
`~/Library/Application Support/Sort X/settings.json`. The app stops by itself a few seconds
after you close its window.

---

## .X: sort files

```
Sorted/
├── Images/          ├── Spreadsheets/    ├── Code/
├── Videos/          ├── Presentations/   ├── Fonts/
├── Audio/           ├── Archives/        ├── Other/
├── Documents/       ├── Installers/      └── Duplicates/   ← review, then delete if you like
```

- **Sorts by file type** into the folders above (unknown types go to `Other`).
- **Optional Year/Month folders** (`Images/2019/07-July/...`). Photos use the date they
  were taken (EXIF) when Pillow is installed, other files use their creation date.
- **Finds exact duplicate files by content**, not by name. It compares size first, then a
  quick hash, then a full SHA-256, so it stays fast on large folders.
- **Keeps the best copy**: prefers files already sorted, then names without
  "copy" / "(1)", then the oldest file.
- **Remembers past runs**: new files dropped into the folder later are compared against
  what's already in `Sorted/`.
- **Safe**: duplicates go to `Duplicates/` with a CSV report, unless you choose **Remove**
  (Trash). Every run writes a log you can **undo**. Undo also brings back files that were
  moved to the Trash, when they're still there.

How to use it:

1. Click the folder card and pick a folder with the normal macOS folder dialog.
2. Turn on the options you want.
3. Click **Preview** to see the plan: a count per category and every file's new location.
   Nothing changes yet.
4. Click **Review duplicates** to check every set of identical files. Beside every
   duplicate: **Keep** (sort it like a normal file), **Remove** (send it to the Trash), or
   neither (it goes to the `Duplicates` folder). **Keep all** / **Remove all** do every one.
5. Click **Sort now**. Use **Undo** if you change your mind.

After sorting, duplicates in the `Duplicates` folder still have **Keep** and **Remove**
buttons, which act right away. Press **Stop** (or Esc) at any time while it's working.

---

## .Image: find duplicate and similar pictures

.Image finds two kinds of matches, each in its own tab.

**Exact duplicates** show **exactly the same pixels**, so .Image finds copies that a file
comparison would miss:

- different file names (`IMG_4471.jpg` and `beach.jpg`)
- different metadata (edited dates, tags, comments)
- lossless format changes (the same picture saved as PNG and TIFF)
- a photo stored sideways with a rotation tag (pictures are compared upright)

**Similar pictures** look alike but aren't identical. Choose how close they must be:

| Setting | Finds |
|---|---|
| **Very close** | resized, re-compressed or re-saved copies |
| **Close** (default) | also brightness/contrast changes, watermarks, small crops, dark/light versions |
| **Loosely alike** | also black-and-white or recoloured copies and bigger crops. It can group different photos that look alike, so check these sets |

Mirrored (flipped) pictures don't count as similar. Similar matching uses a small visual
fingerprint of each picture plus a colour check, tuned on real photos.

How to use it:

1. Pick a folder of pictures, choose what to find, and click **Scan pictures**. Scanning
   only looks.
2. **Exact duplicates** tab: each set shows every copy as a thumbnail with its file name,
   folder, size, date and type. The **Original** is the copy with the clearest name (then
   the oldest). Copies whose file data differs are labelled *Same picture, different file*.
3. **Similar** tab: in each set the **Best quality** copy (most pixels, then the clearest
   name, then the oldest) is the suggested keeper. Every other copy shows its dimensions and
   how alike it is (e.g. *Similar · 97%*).
4. Beside each duplicate or similar picture:
   - **Keep**: leave it where it is.
   - **Remove**: send it to the Trash.
   - **Restore**: bring a removed picture back to its folder.
   - **Make this the original** / **Make this the one to keep**: keep a different copy.

   **Remove all exact duplicates** removes every exact duplicate you haven't marked Keep,
   after asking; similar pictures aren't touched. For similar pictures, each set has
   **Keep best, remove the others**, so you decide set by set.
5. Click any picture to see it full size. Use ← → to compare the pictures in a set and
   Esc to close. The **All pictures** tab shows every picture that was scanned.

Supported: JPG, PNG, GIF, BMP, TIFF, WebP, HEIC/HEIF, AVIF, PSD and camera RAW files
(HEIC and RAW are read with macOS's built-in `sips`).

Previews are cached in `~/Library/Caches/Sort X/previews`; it's safe to delete that folder.

---

## Command line

.X:

```bash
python3 file_sorter.py ~/Downloads --dry-run
```

```bash
python3 file_sorter.py ~/Downloads
```

```bash
python3 file_sorter.py ~/Pictures --by-date --copy -o ~/Pictures_Sorted
```

```bash
python3 file_sorter.py ~/Downloads --undo
```

| Option | What it does |
|---|---|
| `-n`, `--dry-run` | Preview only, change nothing |
| `-o`, `--dest FOLDER` | Where to put sorted files (default: `<folder>/Sorted`) |
| `--copy` | Copy instead of move (originals stay where they are) |
| `--by-date` | Add Year/Month subfolders |
| `--no-dedupe` | Don't look for duplicates |
| `--no-recursive` | Only the top level, ignore subfolders |
| `--include-hidden` | Also sort hidden files |
| `--keep-empty-folders` | Don't remove folders left empty after moving |
| `--undo [LOG]` | Undo the latest run, or a specific `sort_log_*.json` |
| `--gui` | Open the window |

.Image (lists duplicate pictures, changes nothing):

```bash
python3 image_dupes.py ~/Pictures
```

Add similar pictures with `--similar strict`, `--similar balanced` or `--similar loose`:

```bash
python3 image_dupes.py ~/Pictures --similar balanced
```

## Files

| File | What it is |
|---|---|
| `Sort X.command` | Double-click to open Sort X |
| `file_sorter.py` | .X sorting and duplicate logic, command line, Trash helpers |
| `image_dupes.py` | .Image exact and similar picture matching, and previews |
| `gui_server.py` | Runs the window (local server on this Mac only) |
| `ui/index.html` | Sort X home screen |
| `ui/x.html`, `ui/image.html` | The .X and .Image screens |
| `ui/common.css`, `ui/common.js` | Look, header, app switcher and helpers shared by every screen |

## Requirements

- Python 3.8+ (macOS has it built in).
- Pillow (`pip3 install Pillow`) for .Image and for sorting photos by date taken.

To add or change .X file types, edit the `CATEGORIES` table near the top of `file_sorter.py`.
