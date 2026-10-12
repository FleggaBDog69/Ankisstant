# Phase 6 smoke test

A once-through in real Anki before `phase6-groups-and-unification` merges to
`main`. Use a throwaway gap or two and a small lecture so a run takes minutes,
not an hour. Tick each line; anything that breaks, note what you pressed and
what you saw (the Ankisstant log is in `user_files/`).

Before you start: Tools → Add-ons → make sure Ankisstant is **enabled**, then
restart Anki.

## 0. It loads
- [ ] Anki starts with no error popup.
- [ ] The Ankisstant window opens; every tab renders.
- [ ] There is no AI Create page any more, and nothing links to one.
- [ ] No blank grey squares: the **?** beside Gaps/Groups, the **⟳** beside
      KG from AI chat and beside Manage groups… all show their glyph.

## 1. Gaps page — the doors
- [ ] **＋ Add KG** into a group, with notes. Save. It appears in the group.
- [ ] On that gap: **Send to queue** → it shows as queued.
- [ ] **▶ Run queue** opens the work-through on the queued gaps.
- [ ] On another gap: **Find & write now — skip the queue** opens the
      work-through directly.
- [ ] Right-click a group: **Send {group} to queue** and
      **Find & write {group} now…** both do what they say. A group that is
      all Closed says "nothing to run".
- [ ] **✎ Write my own card** still opens the editor for that gap.

## 1b. Gaps page — layout
- [ ] Click a gap: nothing in the detail pane runs off the right edge —
      Delete and "added …" are visible without scrolling sideways.
- [ ] With no gap selected, the pane shows only "Select a knowledge gap…",
      no bare "Type:" / "Tags:".
- [ ] Groups view with no group picked: just the heading and a hint, no
      greyed-out button rows. Pick a group: everything appears.
- [ ] Menus read "Find & write …" with the ampersand.

## 2. Work-through — one gap, end to end
- [ ] READ: the points arrive without you pressing anything.
- [ ] APPROVE: you can edit or untick a point.
- [ ] FIND: the search runs; found cards appear in the tree.
- [ ] WRITE: cards get written for the uncovered points.
- [ ] Card pane: click a found card → every field is shown in full;
      **Open in the Anki browser** goes to that note.
- [ ] Card pane: click a written card → front, back and source are shown;
      **Edit this card…** and **Find an image…** both work.
- [ ] COMMIT: one Apply. Then check in the browser that the found cards are
      tagged and unsuspended, the new cards exist in the right deck with the
      right tags (content / source / audit), and nothing was added twice.
- [ ] The gap is marked done on the Gaps page.
- [ ] Run again with **Write** unticked on every gap: it only searches, Apply
      says "Apply — N found card(s)", and no second window opens afterwards.
- [ ] There is no "Send gaps to AI Create…" button and no global "Write
      cards for whatever the search doesn't find" box on the plan page.

## 3. AI Lecture — the new door
- [ ] The button reads **Find & write ▶** (with the &).
- [ ] Options: "Cap at [40 notes]  ☐ or size the cap from the lecture"; the
      Search notetype box is a sensible width; Pre-tick suggested slides and
      Add notes to cards sit under Options, not down by the button.
- [ ] Choose a short PDF lecture → **Find & write ▶** opens the work-through
      (not the old results page).
- [ ] READ shows the points grouped into the lecture's sections.
- [ ] FIND and WRITE behave as in §2.
- [ ] Side panel: a card shows its suggested slides and Lecture Notes lines.
      **Put on the selected card** attaches one.
- [ ] **Pre-tick suggested slides** ticks them.
- [ ] COMMIT staples the picked slides/notes onto both found AND written
      cards. Check one of each in the browser.
- [ ] **Save the missing points with slide numbers (.md)…** writes a file.
- [ ] Run the same lecture again: it is fast (read cache) and nothing
      duplicates.

## 4. Material without a PDF (replaces AI Create)
- [ ] **Paste screenshot** → run → the screenshot is read (OCR/vision) and
      gets stapled to the cards it taught.
- [ ] Paste plain text → run → treated like a handout.
- [ ] **Add images…** with two images → both are read.

## 5. Manual / paste provider (if you use it)
- [ ] Switch provider to paste. Lecture: **Load the AI's points file →** and
      **Load the AI's relevance file →** round-trip correctly.

## 5b. Settings
- [ ] Open every tab at the default window size: no field or label runs off
      the right edge, and no "Edit" / "Remove" / "Show" button is clipped.
- [ ] Settings → AI → **Models** shows Read, Find, Write, Check and AI search
      in Anki's browser, each on the model it used before (Read/Find
      Sonnet 5, Write Opus 5, Check Haiku 4.5). Save, reopen: unchanged.
- [ ] Tabs are named **Finding cards** and **Writing cards**. Finding cards
      shows "Tag found cards with:" filled with your audit tag.
- [ ] Settings → Tools: one "✨ AI Search box…" tick box, no AI Create switch.
      The ✨ AI Search box still appears in Anki's Browse window.
- [ ] Settings → QBank: "Add an AI-written explanation…" is unticked.

## 5c. Data safety (optional, 1 minute)
- [ ] Use the kg-add skill (or drop a line into `user_files/kg_inbox.json`)
      while Anki is open, restart: the gap arrives once, and there's no
      `kg_inbox.json.draining` left in user_files.

## 6. Leftovers to look at
- [ ] The AI Lecture tab shows only the intake form — no old results pane,
      no **Tag + Unsuspend** button. (They're kept in the code on purpose for
      the standalone LectureLens build and should be hidden here.)
- [ ] **Stop** mid-run stops cleanly, and you can run again afterwards.

When everything is ticked, merge to `main`, bump the version, and move the
Unreleased section in CHANGELOG under it.
