"""Canonical page-status vocabulary shared by extraction (ingest) and the
runtime gap reporter. Kept dependency-free so the serving container can import
it without pdfplumber (pdf_extract re-exports GAP_PAGE_STATUS from here)."""

# statuses that make a page non-indexable — the extraction-gap notice set
GAP_PAGE_STATUS = {"suspect_encoding", "suspect_reading_order", "image_only", "extract_error"}
