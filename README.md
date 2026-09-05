# PDF Library Reader

A desktop PDF reader for Linux, built for reading books: a library view of all
your PDFs plus a reader with bookmarks, adjustable text size, dark/light mode,
and a distraction-free "simple text" reading mode.

## Features

- **Library / main menu** — every PDF you add, shown with title, file size,
  page count and last-read date.
- **Title comes from the filename, not the PDF's internal metadata** — when
  you add a book, its Title (and Author/Series/Genre/Language, if present)
  are read from the filename itself, e.g. adding a file named
  `Dune - Frank Herbert - Dune Saga - Science Fiction.pdf` fills in all
  four fields automatically. A plain filename with no `-` separators is used
  as the title as-is. Either way, the PDF's own embedded metadata is
  ignored, since it's often unreliable — many tools stamp it with whatever
  the document's first heading happened to be, not the actual book title.
  Re-scanning a folder you've already added never overwrites metadata you
  edited by hand.
- **Refresh (F5)** — click **Refresh** in the toolbar, or just press **F5**,
  to re-check the library against what's actually on disk (this also runs
  automatically every time you start the app, and right after you change
  your Library Folder setting — see below). The button briefly shows
  "✓ Refreshed" so you know your click (or F5) actually did something, even
  if nothing changed.

  A book is hidden from the normal view — and counted in a small warning
  like "⚠ 2 books need attention... — click for details" — if either:
  - its file was renamed or deleted outside the app (so its recorded path
    no longer exists), or
  - a Library Folder is configured and the book's file exists but isn't
    inside it, which also covers every book at once right after you clear
    the Library Folder setting, since none of them belong to "no folder"
    either.

  Its library entry (bookmarks, categories, notes) is always kept in case
  the file turns up again later, e.g. on a drive that wasn't connected.
  Clicking the warning opens a list of exactly which books need attention
  and why, where you can select any entries and **Clear Selected**, or use
  **Clear All** for the whole list at once, to remove just their library
  entries (never an actual file that still exists); **Move N Into Library
  Folder** relocates the ones that are just in the wrong place (shown only
  when a folder is actually configured); **Remove All N Missing** clears
  out the ones that are genuinely gone for good.
- **Library Folder** — click **Library Folder...** in the toolbar to see
  whether one's set, **Change...** to pick a folder for your whole library
  to live in, or **Clear** to go back to books staying wherever you
  originally added them from. Once a folder is set:
  - **Add Book(s)** and **Add Folder** move newly added PDFs into it (flat
    — no subfolders kept) instead of leaving them wherever you picked them
    from; a name collision gets a "(1)", "(2)"... suffix rather than
    overwriting anything.
  - Every refresh also scans the folder itself for PDFs the library doesn't
    know about yet — including ones sitting in a subfolder, which get
    flattened up to the top level — so anything you drop in there yourself
    from outside the app shows up automatically on the next refresh (or
    immediately, right after you point the folder at one that already has
    books in it).
  - Importing a **Full Archive** extracts straight into it too, skipping
    the "choose a folder" prompt.
- **Two library views** — **Simple Text**, the detailed list (title, size,
  page count, last read), and **Image Preview**, a grid of page-1 thumbnails
  like a bookshelf. Thumbnails are generated once and cached to disk, so
  later visits load instantly. In Image Preview, double-click a cover to
  open it, or right-click for Open / Toggle Favorite / Remove.
- **Search Text in Library** — searches the actual text content of every
  book, not just titles. Runs in the background so the app stays responsive,
  shows progress while it scans, and lists every match with the book title,
  page number and a snippet of surrounding text. Double-click a result to
  jump straight to that page in that book.
- **Sort / filter** — Title (A-Z / Z-A), Recently Read, or File Size
  (largest/smallest first), plus a live filter box that matches titles (use
  "Search Text" instead to search inside books).
- **Favorites** — star any book; switch to a "Favorites only" view with one
  click.
- **Book details** — right-click a book (its row in Simple Text view, or its
  cover in Image Preview) and choose **Details** to open a preview panel
  where you can edit Title, Author, Series, Genre, Language and a free-text
  Annotation, and set its reading status. Saving updates the library
  immediately. Kept off a plain click on purpose, so browsing your library
  doesn't keep popping the panel open.
- **Filename stays in sync with Title / Author / Series / Genre / Language**
  — saving details renames the actual file on disk to
  `Title - Author - Series - Genre - Language.pdf` (empty parts are just
  dropped, e.g. `Title.pdf` if nothing else is set yet). A book with more
  than one genre or language shows up as e.g. "Science Fiction_Fantasy" or
  "English_Bulgarian" right in the filename, and is found when filtering or
  searching for any one of them on its own. That way the metadata travels
  with the file itself — copy your library folder to another device and
  it's all right there in the filenames, no re-entering anything by hand.
  Illegal filename characters are stripped automatically, and a name that
  would collide with an existing file gets a "(2)" appended instead of
  overwriting it.
- **Search suggestions** — start typing in the filter box and a preview
  drops down grouping matches into **Titles**, **Authors**, **Series**, and
  **Genres** (e.g. typing "herbert" shows "Frank Herbert (3 books)" under
  Authors). Click any suggestion to jump straight to all matching books.
- **Hover highlight** — hovering a book (its row in Simple Text view, or its
  cover in Image Preview) shows a light blue tint and outline around it.
- **Favorites get a corner star** — a gold star badge appears on a
  favorited book's cover in Image Preview, so favorites are visible at a
  glance without opening anything.
- **Reading status** — four statuses: Unread, To Read, Reading, and
  Finished. Set any of them from a book's right-click **Mark as** submenu
  (also available for a bulk selection via **Select** mode), or from the
  details panel. Opening a book automatically promotes it to "Reading" from
  either Unread or To Read; mark it "Finished" from the reader toolbar or
  the details panel. In Image Preview each status shows as a small colored
  icon badge in the corner of the cover — a purple bookmark ribbon for To
  Read, a blue play triangle for Reading, a green checkmark for Finished —
  and a matching chip next to the title in Simple Text view.
- **Status filter** — a "Status" dropdown next to the filter box narrows the
  library to None (no filter), To Read, Currently Reading, Finished, or
  Unread — combinable with Favorites and the title filter.
- **Alphabetical grouping with a jump-to-letter bar** — in Image Preview,
  sorting by Title (A-Z or Z-A) groups covers under letter headers (A, B,
  C... with "#" for titles starting with a number or symbol), with a
  clickable A-Z index strip pinned above the grid so you can jump straight
  to any letter. Letters with matching books are clickable; letters with
  nothing yet are grayed out. The bar only appears while sorted
  alphabetically — it stays on top even when the Genre/Language filter bar
  (below) is also turned on, so a big library can use both together.
- **Genre / Language filtering** — turn on **Genres & Languages** in the
  toolbar to show a filter bar underneath the A-Z index: a **Genre** dropdown and a
  **Language** dropdown side by side, each letting you check any number of
  options at once (matches are OR'd together). Click anywhere on either box
  to open it, not just the little arrow — and the popup stays open across
  clicks so you can check several without reopening it, working the same
  way even if a pick happens to match zero books, so you can freely adjust
  your filter without the menu closing or the bar disappearing. A book with
  more than one genre or language (e.g. "English_Bulgarian") matches if it
  has *any* of the values you've selected. Genre and Language filters
  combine with each other (AND) and with everything else — search, status,
  category. **Clear Filters** deselects everything at once; turning the
  toggle off hides this bar again (the A-Z bar, if applicable, was never
  hidden). Both dropdowns come
  with a comprehensive preset list (40+ genres, 35+ languages) so there's a
  consistent vocabulary to filter by, but any custom value you've typed in
  Book Details shows up as a filter option too.
- **Pagination** — a "Per page" dropdown next to the sort box lets you split
  the list into pages of 10/25/50/100 instead of showing everything at once,
  with Previous/Next navigation at the bottom — works for every sort mode,
  including Title, which is worth turning on for large libraries since
  rendering hundreds of covers at once can get slow and memory-hungry. Under
  Title sort, the A-Z bar still shows every letter with a match across your
  whole library, not just the current page — click one that's on a different
  page and it jumps you straight there automatically. Defaults to "All"
  (today's behavior). The page resets to 1 whenever you change the search,
  filters, sort, or category, so you never land on an empty page.
- **Categories** — organize your library into your own custom categories,
  shown in a sidebar on the left with a live book count for each (and a
  star for ones you've favorited). Selecting **All Books (None)** shows
  everything as normal; selecting a category filters the list to just its
  books. Right-click a category to **Add Books...** — search by **Title**
  to add one specific book, or by **Author**/**Series** to add every
  matching book at once, with a **Text** / **Image Preview** toggle so title
  matches can show as cover thumbnails — or right-click a category to
  favorite, rename, or delete it (deleting a category never deletes the
  books themselves). Turn on **Select** in the toolbar to multi-select
  books; with Select off, clicking a book does nothing, so you can browse
  normally without ever selecting one by accident. Once it's on:
  - **Click** a book to select/deselect it
  - **Shift+click** another book to select everything between it and the
    last one you clicked, like most file managers
  - **Ctrl+A** selects every book currently on screen (just the current
    page, if you're paginated)
  - **Click empty space** clears the whole selection

  While selected, right-click any selected book to add the whole selection
  to a category, **set the Series/Genre/Language** for every selected book
  at once (a quick way to tag a batch of books in one go), **export just
  their categories** to a JSON file, or remove them all from the library at
  once — works in both Simple Text and Image Preview. The selection
  automatically clears itself once you commit an action like this, so it's
  ready for the next thing without an extra click.

  The **category list itself** also supports multi-select — Ctrl+click,
  Shift+click for a range, or Ctrl+A — for bulk-favoriting, bulk-deleting,
  or bulk-exporting several categories at once via the right-click menu
  (a plain click still just filters by that one category, as before).

  **Export...** in the toolbar opens a single dialog rather than a
  scoped menu of separate options — what goes in and which books are
  included are now two independent, straightforward choices instead of
  one combined menu pick:
  - **Scope is automatic, not chosen in the dialog.** No books selected
    when you click Export → every book in your library. Some books
    selected first (via **Select** mode — click, Ctrl+click, Shift+click
    for a range) → just those. The dialog's own heading always confirms
    which one applies ("Exporting all 42 books" or "Exporting 3 selected
    books") before you commit to anything.
  - **Content is checkboxes.** Each book's free-text annotation always
    travels along — it's small enough that there's no real case for
    wanting a copy without it — and six pieces are independently
    optional: **PDF Files**, **Categories**, **Bookmarks**, **Highlights
    & Drawings** (bundled together, since the app already shows them as
    one unified list), **Reading Status** (unread/reading/finished and
    favorites), and **Reading Progress** (last page read). All six start
    checked.
    - Uncheck everything except **PDF Files** to share books with someone
      else without handing over your own categories, notes, or reading
      history — they shouldn't receive books mysteriously pre-marked
      "Finished", already favorited, sorted into categories that only
      make sense in your own library, or covered in your own highlights
      and drawings.
    - Uncheck **PDF Files** itself for a lightweight, metadata-only
      export instead — the file is still a `.zip`, just without the PDFs
      themselves inside it, useful for syncing categories/bookmarks/etc.
      between installs that already share the same PDF files (matched by
      filename on import, same as everything else here). This is what the
      old separate "Categories Only" and "Bookmarks Only" actions used to
      be — now it's just these checkboxes instead of separate menu
      items.
    - Leave everything checked for a full personal backup, or pick
      whatever mix actually fits the moment.

  For a narrower export, right-click a category (or Ctrl+click/Shift+click
  to select several first) and choose **Export...** there instead of the
  toolbar's whole-library version — it opens the same dialog, scoped to
  that category's books. **Import...** stays generic regardless of any of
  this — it reads whatever's actually in the file and applies it
  automatically (including files exported by an older version of this
  app, in the old separate Categories/Bookmarks JSON formats — those
  still import correctly even though nothing produces them anymore).
  Every import matches books already in your library by filename and
  reports how many matched vs. weren't found; re-importing the same file
  is always safe (nothing gets duplicated). All of this is deliberately
  manual and explicit rather than an always-on background sync, so two
  devices working from a shared/synced folder can't silently overwrite
  each other's data without you choosing to do it.
- **Bookmarks** — save a bookmark (with an optional label) on any page inside
  a book, jump back to it later, remove it when you're done. The panel has
  its own "Bookmarks" toggle button next to "+ Bookmark" in the toolbar, so
  if you close the panel with its own [x] button, that same toggle brings it
  right back. **Highlights** (see below) live in the same panel, right below
  your bookmarks, so both are easy to browse together.
- **Text size** — A+ / A- controls zoom in normal view and font size in
  simple text mode. Your last setting is remembered.
- **Click-and-drag panning** — when you've zoomed in past what fits the
  window, left-click and drag the page to move around it, like a normal
  image viewer. The cursor shows an open hand when a page can be panned, and
  a closed hand while actively dragging. Disabled in Simple Text mode (there's
  nothing to pan — text just wraps to fit).
- **Scroll-to-change-page only when it's safe to** — plain mouse scroll turns
  the page while "Fit to Screen" is on, or in Simple Text mode once you hit
  the top/bottom edge. The moment you zoom in manually, plain scroll only
  pans around the page — it never flips pages by accident anymore. Hold the
  **middle mouse button** while scrolling to explicitly turn the page even
  while zoomed in.
- **Fit to Screen** — on by default. Each page is automatically scaled to
  fill the window, recalculated per page, so pages of different sizes within
  the same book (tall, wide, mixed scans...) all display at a sensible size
  without you doing anything. Click A+ / A- at any time to take manual
  control of the zoom instead; click "Fit to Screen" again to go back to
  automatic.
- **Dark Mode and Dark Pages are independent** — "Dark Mode" is the app's own
  theme (toolbars, menus, text mode). A separate "Dark Pages" toggle inverts
  the colors of the rendered page for a proper night-reading mode. Mix and
  match: dark app with normal pages, light app with inverted (dark) pages,
  both, or neither — whatever's comfortable.
- **Simple text mode** — strips away the page layout and shows just the
  extracted text of the page, reflowed to your chosen font size — good for
  text-heavy books, bad for pages that are mostly images/diagrams. Its text
  can be selected and copied directly, like any normal text view.
- **Select Text** — click "Select Text" in the reader toolbar, then drag over
  the rendered page like you would in Adobe Acrobat or any other PDF viewer.
  Selection is character-level, so it can start or end mid-word, not just
  snap to a whole word at a time — and it follows real reading order rather
  than a blocky rectangle, so dragging from partway through one line to
  partway through a line several lines down correctly grabs every full line
  in between, not just whatever happens to fall inside the drag box. A
  genuine two-column academic-paper-style layout is detected and read left
  column fully, then right column fully, rather than however the PDF's own
  internal ordering happens to interleave them. Works the same way across a
  Two-Page View spread, including a drag that starts on one page and ends
  on the other. A hyphen at the end of a wrapped line is detected and
  removed from copied text so words rejoin correctly ("tele-" / "photo" →
  "telephoto"), while a genuine hyphenated word that happens to also end a
  line is left alone. Double-click selects a word, triple-click selects the
  whole paragraph. Dragging near the top/bottom edge of the window
  auto-scrolls so you can extend a selection past what's currently visible.
  A small popup appears next to a finished selection with **Copy** and
  **Search in Book** right there; the same options (plus **Select All** and
  **Save Highlight...**) are on the right-click menu, and Ctrl+C / Ctrl+A
  work too. If a page has no extractable text at all (a scanned image with
  no OCR layer, most likely), a small message says so instead of leaving
  you to wonder why nothing's happening. Not available in Simple Text mode,
  since that view's plain text can already be selected and copied directly.
- **Highlights** — right-click a selection (or use the "Save Highlight"
  button in the selection popup) to pick a color and a style — a solid
  highlighter-style **fill**, an **underline**, a **strikethrough**, or
  fill combined with either accent line, the
  same distinct annotation tools a real PDF editor offers — and keep it
  permanently on the page. The "Highlight Color" toolbar button sets the
  default color used for both the live selection and new highlights, so
  you're not starting from scratch every time. Saved highlights show up whenever you're reading
  that page, whether or not Select Text mode is on, and scale correctly
  with zoom. Right-click an existing highlight (on the page, or in the
  sidebar list) for **Delete** or **Edit Highlight...**, which lets you
  change its name, color, and style, and shows a read-only preview of the
  highlighted text so you can tell highlights apart without jumping to the
  page. Naming defaults to "Page N" if you leave it blank, or "Page N - 1",
  "Page N - 2"... for additional highlights on the same page. Every
  highlight also appears in the Highlights section of the bookmarks panel,
  right below your bookmarks, so you can jump straight to it later — and a
  dedicated **Export Highlights...** button there saves everything you've
  highlighted in that book as a standalone Markdown notes file, similar to
  Kindle's "My Clippings" — handy for actually revisiting what you
  highlighted, separately from the archive export/import (which moves your
  library around and carries highlights and drawings along automatically,
  but only in the export variants that include your reading data, not the
  Share ones).
- **Draw** — a manual, freehand alternative/companion to text highlighting:
  pen strokes plus box, circle, triangle, and line shapes, drawn directly
  on top of the page. Since it marks up pixel positions rather than
  recognized text, it works on any page, including a scanned page with no
  text layer at all — the one case regular Select Text highlighting can't
  help with. Click **Draw** in the toolbar to open a second toolbar row
  with the five tools, a color swatch, an opacity slider (with a live "N%"
  readout next to it), and a pen/outline width control — your last-used
  choices are remembered across sessions, the same as the highlight color.
  Draw mode and Select Text mode are mutually exclusive (turning one on
  turns the other off, since both bind a plain click-drag on the page to
  something different); Simple Text mode disables drawing entirely, for
  the same reason it disables Select Text mode.

  Drawing works as a draft, the same shape as text highlighting (drag to
  select text, then click Save Highlight to make it permanent): nothing
  you draw is saved until you click **Save Drawn Highlight**. Draw as many
  strokes and shapes as you like first — **Undo** (or Ctrl+Z, customizable
  like any other shortcut) removes the most recent one, **Clear** discards
  everything drawn since the last save on that page — and if you leave
  Draw mode, switch to Select Text mode, or turn the page without saving,
  the draft simply disappears, the same as an unsaved text selection.

  Once saved, a drawing shows up in the same **Highlights** list in the
  Bookmarks/Highlights panel as text highlights do — sorted together by
  page, not in two separate groups — so it's all in one place rather than
  drawings only being reachable by right-clicking them on the page. From
  that list, double-click jumps to its page, right-click offers **Edit
  Drawing...** (color, opacity, and width — its shape and position aren't
  editable this way, so correcting those still means deleting and
  redrawing) and **Delete Drawing**, and **Remove selected highlight**
  works on a selected drawing entry exactly like it does on a text one.
  Ctrl+]/Ctrl+[ (jump to next/previous highlight) cycle through drawings
  too, in the same page order as the list. Right-clicking a drawing
  directly on the page (only reachable while Draw mode is on, same
  convention as right-clicking a highlight only working in Select Text
  mode) still works as a shortcut for the same delete. Under the hood,
  drawings and highlights stay in separate database tables — a highlight
  is stored as a list of rectangles, and forcing a drawn ellipse or
  triangle through that same representation would flatten it into a
  plain rectangle, losing its actual shape — but that's invisible from
  the panel, which presents both as one unified, page-ordered list.
- **Two-Page View** — shows two pages side by side like a book spread,
  handy on a wide screen. Prev/Next, jumping to a specific page, and
  scrolling past the edge all move by the full spread rather than one page
  at a time. Not available in Simple Text mode.
- **Password-protected PDFs** — opening one prompts for its password; a
  correct one unlocks it for that session. The same dialog optionally lets
  you permanently remove the password from the file, or change it to a new
  one, right at the point of unlocking.
- **Corrupted files are caught, not crashed on** — a file that can't
  actually be opened gets a small red warning triangle on its cover in
  Image Preview, and attempting to open it shows a clear "this file is
  corrupted" message instead of an error dump. (A merely password-protected
  file is treated as locked, not corrupted — very different situations.)
- **Sort by Recently Added** — alongside Title, Recently Read, and File
  Size, sort your library by when each book was added, most recent first.
- **Reading progress** — automatically remembers the last page you were on
  for each book, so "Open" picks up where you left off.

## Requirements

- Linux with a desktop environment (X11 or Wayland)
- Python 3.9+

## Install

```bash
chmod +x install.sh   # if it isn't already executable
./install.sh
```

This creates a `.venv` virtual environment in this folder, installs the two
dependencies (PySide6 for the UI, PyMuPDF for PDF rendering), and adds a
"PDF Library Reader" entry to your applications menu.

## Run

Either launch it from your applications menu, or:

```bash
./run.sh
```

## Uninstall

```bash
rm -rf .venv run.sh
rm ~/.local/share/applications/pdf-library-reader.desktop
```

Your library database (list of books, bookmarks, favorites, reading
progress) lives separately at `~/.local/share/pdf-library-reader/library.db`,
and cached cover thumbnails live at
`~/.local/share/pdf-library-reader/thumbnails/` — delete these too if you
want a completely clean slate. Removing a book from the library never
deletes the underlying PDF file.

## Keyboard shortcuts (inside a book)

| Shortcut       | Action                        |
|----------------|--------------------------------|
| ← / →          | Previous / next page          |
| Scroll         | Turn page (when Fit to Screen is on) or pan (when zoomed in) |
| =              | Increase text size / zoom in  |
| -              | Decrease text size / zoom out |
| F              | Toggle Fit to Screen          |
| Ctrl + D       | Add a bookmark on this page   |
| Ctrl + Z       | Undo the last drawing stroke (while in Draw mode) |
| Click + drag   | Pan around a zoomed-in page   |
| Right-click    | Opens a menu (Select Text / Draw / Add Bookmark) — see below |

Every shortcut in this table except the Scroll-based ones (mouse-wheel
interactions, not part of the customizable catalog) can be changed or
cleared in the Keyboard Shortcuts dialog — including the zoom keys,
if you'd rather have them require a modifier (e.g. `Ctrl+=`/`Ctrl+-`)
than the bare `=`/`-` keys they default to.

## Mouse wheel gestures

Three gestures — Ctrl held while scrolling, or Middle-Click / Right-Click
held while scrolling — are each independently configurable to one of
three actions: **Zoom In/Out**, **Turn Page While Zoomed**, or **None**.
(Shift+Scroll and Alt+Scroll are deliberately not offered here, since
both already carry OS-level meaning on most systems — Alt+Scroll commonly
changes the scroll axis to horizontal, for instance — and claiming them
for something else here would fight with behavior you already have
outside this app.) By default:

| Gesture                        | Action               |
|---------------------------------|----------------------|
| Ctrl + Scroll                   | Zoom In/Out          |
| Right-Click (hold) + Scroll     | Zoom In/Out          |
| Middle-Click (hold) + Scroll    | Turn Page While Zoomed |

Unlike the keyboard shortcuts above, more than one gesture can point at
the same action at once on purpose — Ctrl+Scroll and Right-Click+Scroll
both zoom simultaneously by default, so use whichever's more natural in
the moment, or reassign either in the **Mouse Wheel Gestures** section
of the Keyboard Shortcuts dialog. That section has no conflict warning,
because two gestures sharing an action isn't a conflict here the way two
keyboard shortcuts sharing a key would be.

Right-click held through a scroll — using it for the zoom gesture above,
if that's how it's configured — never also opens the right-click menu
described below on release; only an actual simple right-click does.

## Right-click reading menu

Right-clicking the page while just reading (not already in Select Text
or Draw mode — those have their own right-click menus, for editing or
deleting whatever you clicked on) opens a quick menu: **Select Text**,
**Draw**, and **Add Bookmark**. It exists for exactly the moments a
keyboard isn't within reach — reading with your hands on a mouse or
trackpad rather than a keyboard, notebook on the desk instead.

## Book details panel

Right-click a book (its row, or its cover thumbnail) and choose **Details**
to open the panel. From there you can:
- Edit **Title**, **Author**, **Series**, and a free-text **Annotation**
  (your own notes about the book)
- Pick one or more **Genres** from the dropdown (check as many as apply —
  the box shows a summary like "Science Fiction, Fantasy" when closed),
  plus check **Custom** to add one more genre that isn't in the list
- Pick one or more **Languages** the same way (check as many as apply —
  shows e.g. "English, Bulgarian" when closed), plus check **Custom** to
  add one more language that isn't in the list
- Set its **Status** directly (Unread / To Read / Reading / Finished)
- Toggle **Favorite**
- Jump straight into **Open Book**

Changes only take effect once you click **Save**. Saving also renames the
file on disk to match Title/Author/Series/Genre/Language (see above) — if
that rename fails for some reason (e.g. the file was moved externally), your
details are still saved and you'll get a warning explaining the file itself
couldn't be renamed.

## Notes / limitations

- Dark mode's page inversion is a simple full-page color invert (the common
  "night mode" trick). It looks great for text pages; photos or heavily
  colored pages will look inverted too, not color-corrected.
- Simple text mode works page-by-page, extracting whatever text PyMuPDF can
  find on that page. Scanned/image-only PDFs won't have extractable text.
- "Search Text in Library" opens and scans every book on demand (it doesn't
  keep a permanent search index), so the first search after adding a lot of
  books may take a few seconds. Scanned/image-only PDFs won't have any
  searchable text, same as with simple text mode.
- The app doesn't modify your PDF files — bookmarks, favorites and reading
  progress are stored separately in the local database, not written into the
  files themselves.
