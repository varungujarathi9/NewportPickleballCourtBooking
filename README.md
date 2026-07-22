# Newport Racquet Club auto-booker

Books your Ground Level Courts slot the moment it opens up (7 days out),
scheduled via cron.

## 0. Install dependency

```bash
pip install requests --break-system-packages
```

## 1. One-time: extract your Firebase refresh token (locally, never via chat)

This is the credential that lets the script mint fresh login tokens without
you re-entering a password. Do this on the same computer that will run cron,
while logged into the booking site in Chrome.

1. Open https://newportracquetclub.podplay.app/book and make sure you're logged in.
2. Open DevTools (Cmd+Option+J / F12) → **Console** tab.
3. Paste this and press Enter. It reads the token straight from the browser's
   IndexedDB and downloads it as a file — it is never displayed on screen or
   sent anywhere else:

   ```js
   (async () => {
     const req = indexedDB.open('firebaseLocalStorageDb');
     req.onsuccess = () => {
       const db = req.result;
       const tx = db.transaction('firebaseLocalStorage', 'readonly');
       const store = tx.objectStore('firebaseLocalStorage');
       const all = store.getAll();
       all.onsuccess = () => {
         const entry = all.result.find(e => e.value && e.value.stsTokenManager);
         if (!entry) { console.error('No auth entry found - are you logged in?'); return; }
         const refreshToken = entry.value.stsTokenManager.refreshToken;
         const blob = new Blob(
           [JSON.stringify({ refresh_token: refreshToken }, null, 2)],
           { type: 'application/json' }
         );
         const a = document.createElement('a');
         a.href = URL.createObjectURL(blob);
         a.download = 'podplay_auth.json';
         a.click();
         console.log('Downloaded podplay_auth.json - move it to ~/.podplay_auth.json');
       };
     };
   })();
   ```

4. Move the downloaded file into place:

   ```bash
   mv ~/Downloads/podplay_auth.json ~/.podplay_auth.json
   chmod 600 ~/.podplay_auth.json
   ```

This refresh token stays valid until you sign out everywhere or Google/PodPlay
revokes it — you shouldn't need to redo this often, but if the script ever
reports a 401 that survives a retry, redo this step.

## 2. Check the sessions payload shape (do this once)

I built `is_target_slot_open()` from the endpoint URL structure, but not from
a real success response body (I only captured the request, not the response
JSON). Before relying on this for a real early-morning race, run:

```bash
python3 book_court.py --dump-sessions
```

Look at the printed JSON for the field names indicating a time slot's start
time and open-court count, and adjust `is_target_slot_open()` in
`book_court.py` if they don't match what the function currently expects
(`startTime`/`start`, `availableTables`/`openCourts`).

## 3. Test without booking

```bash
python3 book_court.py --dry-run
```

This does everything except the final POST, and prints what it *would* have
sent.

## 4. Schedule it

Since you want the same time slot (00:00 / midnight) every day, 7 days out,
and the new day appears to unlock at midnight the club's local time, run this
just after midnight:

```bash
crontab -e
```

Add:

```
1 0 * * * /usr/bin/python3 /path/to/book_court.py >> /path/to/booklog.txt 2>&1
```

Adjust the path and confirm `python3`'s location with `which python3`. The
script itself polls every 5 seconds for up to 15 minutes in case the slot
takes a moment to unlock, so the cron time doesn't need to be exact to the
second.

## 4b. Or schedule it via GitHub Actions instead

If you'd rather not rely on a machine at home staying powered on, the repo
includes `.github/workflows/book-court.yml`, which runs on GitHub's own
servers every day at midnight US Eastern time. **GitHub Actions cron is
always UTC**, and Eastern's UTC offset changes with daylight saving, so the
workflow schedules two triggers - `0 5 * * *` (00:00 EST) and `0 4 * * *`
(00:00 EDT) - and a guard step checks the actual Eastern wall-clock hour at
runtime and skips whichever trigger doesn't land on local midnight that day.
Only one booking run actually executes per day. If the club is in a
different timezone, adjust both the cron lines and the `TZ=` value in that
guard step.

Since Actions runners don't have access to `~/.podplay_auth.json` on your
machine, you provide it as a repository secret instead:

1. Push this repo to GitHub (it can be a private repo).
2. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret**.
3. Name it `PODPLAY_AUTH_JSON`, and paste the *entire contents* of your local
   `~/.podplay_auth.json` file as the value (it's just `{"refresh_token":
   "..."}`).
4. The workflow writes that secret to `~/.podplay_auth.json` on the runner at
   the start of each run, then executes `book_court.py` exactly as it would
   run locally.

You can trigger a manual test run anytime from the **Actions** tab → "Book
court" → **Run workflow**, with the "Dry run" checkbox enabled to verify
everything works without submitting a real booking.

Treat `PODPLAY_AUTH_JSON` like a password: anyone with write access to the
repo's secrets (or push access, if you ever log the token) could use it to
book or act on your account. Rotate it (redo the extraction step) if you ever
suspect it leaked.

## Notes / limits

- `GROUP_SIZE`, `DURATION_MINUTES`, and `BOOKING_HOUR` are all in the CONFIG
  section at the top of `book_court.py` - edit them to match what you
  actually want each day.
- If the club changes its booking horizon (currently assumed 7 days) or adds
  bot/rate-limit detection, this will need adjusting.
- The Firebase API key in the script is the public client key (safe to be in
  source) - not a secret credential.
