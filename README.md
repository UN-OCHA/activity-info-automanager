# ActivityInfo Manager

A minimal NiceGUI app: enter a base URL and API token, click Connect, and select
one database from the alphabetized list, displayed as `Name (cuid)`. Its forms
load automatically below it using Get Database Tree and are also alphabetized and
displayed as `Name (cuid)`, including forms in folders and subforms.
The form list is single-select, stays blank without a selected database, and
resets its selection whenever the database changes. No changes are sent to the API.
Selecting a form fetches its schema and shows it below the form list as read-only,
pretty-printed JSON. The schema is cleared whenever the form or database changes.

```powershell
uv sync --locked
uv run --locked python main.py
```

Open http://127.0.0.1:8080 in your browser. The default API base URL is
`https://www.activityinfo.org/resources`. Tokens are held in the browser session's
server-side UI state, not written to a file. Each browser session owns one
`ActivityInfoService` and reuses its authenticated HTTP client. Changing either
connection field closes that client and clears the database list; the client is
also closed when NiceGUI deletes the browser session.

The generated ActivityInfo client is pinned to Git tag `v2.0.0` and uses
`activity_info_api_client` imports and its generated async endpoint functions, as described in
[its README](https://github.com/UN-OCHA/activity-info-python-client/tree/v2.0.0#usage).
The app calls its `get_user_databases` endpoint, which implements
[Get databases](https://www.activityinfo.org/support/docs/api/reference/getDatabases.html).

Code is split into the launcher (`main.py`), API access (`activity_manager/api.py`),
page layout (`activity_manager/page.py`), and native single-select listbox
(`activity_manager/listbox.py`). The native select control supports multiselect
for a future extension; names are displayed while database IDs are kept as values.

Run offline checks with `uv run --locked python -m unittest -v`.
