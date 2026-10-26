"""Limits of the storage browser, in one place: settings defaults read them too."""

#: Most entries of one folder the browser can page through. Listing a folder
#: reads every name in it (cloudpathlib listings cannot start mid-way), so this
#: bounds the memory a huge folder can take; entries past it are not reachable.
MAX_LIST_ENTRIES = 100_000
#: Page size of a listing: the default, and the most a client may ask for.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
#: Default of `storage_upload_file_max_size`.
DEFAULT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
#: Largest file the text editor reads or saves.
MAX_TEXT_BYTES = 2 * 1024 * 1024
#: Buffer size for copying file contents, in and out.
COPY_CHUNK = 1024 * 1024
