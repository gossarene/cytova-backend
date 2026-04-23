"""
Cytova — Label Print Defaults

Hard-coded defaults for each print mode. These values seed the two
system ``LabelPrintPreset`` rows (see the data migration) and are also
exposed to the frontend via the ``GET /lab-settings/label-defaults/``
endpoint so forms can pre-fill sensible dimensions when a laboratory
switches print modes.

Mode: A4_SHEET
    Standard A4 portrait, 2 columns × 5 rows (10 labels per page).
    Label size 90 × 50 mm, 10 mm outer margins, 5 mm inner gap —
    matches the most common adhesive A4 label sheet layout sold for
    laboratory specimen tubes.

Mode: THERMAL_ROLL
    40 × 25 mm label, 2 mm gap between labels — typical Zebra / Dymo
    roll configuration used for thermal tube labeling.
"""

A4_DEFAULTS = {
    'page_width_mm': 210,
    'page_height_mm': 297,
    'label_width_mm': 90,
    'label_height_mm': 50,
    'margin_top_mm': 15,
    'margin_left_mm': 10,
    'horizontal_gap_mm': 5,
    'vertical_gap_mm': 5,
    'thermal_gap_mm': 0,
    'show_barcode': True,
    'show_numeric_code': True,
}


THERMAL_DEFAULTS = {
    'page_width_mm': 40,
    'page_height_mm': 25,
    'label_width_mm': 40,
    'label_height_mm': 25,
    'margin_top_mm': 0,
    'margin_left_mm': 0,
    'horizontal_gap_mm': 0,
    'vertical_gap_mm': 0,
    'thermal_gap_mm': 2,
    'show_barcode': True,
    'show_numeric_code': True,
}


DEFAULTS_BY_MODE = {
    'A4_SHEET': A4_DEFAULTS,
    'THERMAL_ROLL': THERMAL_DEFAULTS,
}


def get_defaults(mode: str) -> dict:
    """Return a copy of the defaults dict for the given print mode."""
    if mode not in DEFAULTS_BY_MODE:
        raise ValueError(f'Unknown label print mode: {mode!r}')
    return dict(DEFAULTS_BY_MODE[mode])
