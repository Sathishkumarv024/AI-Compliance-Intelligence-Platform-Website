"""Custom CSS injected on top of Streamlit's base styling for a cleaner,
more enterprise look -- card-style metrics, tighter spacing, a defined
color system for status badges. Kept as plain CSS (no external UI kit
dependency) for the same install-reliability reasons as the rest of this
project."""

import streamlit as st

PRIMARY = "#1a3d5c"       # deep slate blue -- primary brand color
PRIMARY_LIGHT = "#2d5a80"
ACCENT = "#0ea5a0"        # teal accent
PASS_COLOR = "#1a7f37"
FAIL_COLOR = "#c9302c"
INCOMPLETE_COLOR = "#b8860b"
BG_LIGHT = "#f7f9fb"
BORDER = "#e2e8f0"


def inject_css(dark_mode=False):
    bg = "#0f1720" if dark_mode else BG_LIGHT
    card_bg = "#1c2733" if dark_mode else "#ffffff"
    text = "#e6edf3" if dark_mode else "#1a202c"
    border = "#2d3947" if dark_mode else BORDER

    st.markdown(f"""
    <style>
        .stApp {{
            background-color: {bg};
            color: {text};
        }}

        /* ---- Sidebar ---- */
        section[data-testid="stSidebar"] {{
            background-color: {PRIMARY};
        }}
        section[data-testid="stSidebar"] * {{
            color: #f0f4f8 !important;
        }}
        section[data-testid="stSidebar"] .stButton button {{
            width: 100%;
            text-align: left;
            background-color: transparent;
            border: none;
            color: #dbe4ee !important;
            font-size: 0.95rem;
            padding: 0.55rem 0.8rem;
            border-radius: 8px;
            transition: background-color 0.15s ease;
        }}
        section[data-testid="stSidebar"] .stButton button:hover {{
            background-color: {PRIMARY_LIGHT};
        }}

        /* ---- Buttons everywhere else: bigger, rounded, clearer hierarchy ---- */
        .stButton button, .stDownloadButton button, .stFormSubmitButton button {{
            border-radius: 8px;
            padding: 0.55rem 1.3rem;
            font-weight: 600;
            font-size: 0.95rem;
            border: 1px solid {border};
            transition: transform 0.08s ease, box-shadow 0.15s ease;
        }}
        .stButton button:hover, .stDownloadButton button:hover, .stFormSubmitButton button:hover {{
            box-shadow: 0 2px 8px rgba(0,0,0,0.12);
            transform: translateY(-1px);
        }}
        /* Primary action buttons (type="primary") -- accent color, stands out from secondary buttons */
        button[kind="primary"], button[kind="primaryFormSubmit"] {{
            background-color: {ACCENT} !important;
            border-color: {ACCENT} !important;
            color: white !important;
        }}
        button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover {{
            background-color: #0c8a86 !important;
        }}
        .stDownloadButton button {{
            background-color: {PRIMARY} !important;
            color: white !important;
            border-color: {PRIMARY} !important;
        }}
        .stDownloadButton button:hover {{
            background-color: {PRIMARY_LIGHT} !important;
        }}

        /* ---- File uploader: more visually prominent drop zone ---- */
        section[data-testid="stFileUploaderDropzone"] {{
            border: 2px dashed {ACCENT} !important;
            border-radius: 10px !important;
            background-color: {ACCENT}0d !important;
        }}

        /* ---- Cards / metrics ---- */
        .metric-card {{
            background-color: {card_bg};
            border: 1px solid {border};
            border-radius: 10px;
            padding: 1rem 1.25rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }}
        .status-badge {{
            display: inline-block;
            padding: 0.35rem 1rem;
            border-radius: 20px;
            font-weight: 700;
            font-size: 1.1rem;
            letter-spacing: 0.05em;
        }}
        .status-pass {{ background-color: {PASS_COLOR}22; color: {PASS_COLOR}; border: 1px solid {PASS_COLOR}; }}
        .status-fail {{ background-color: {FAIL_COLOR}22; color: {FAIL_COLOR}; border: 1px solid {FAIL_COLOR}; }}
        .status-incomplete {{ background-color: {INCOMPLETE_COLOR}22; color: {INCOMPLETE_COLOR}; border: 1px solid {INCOMPLETE_COLOR}; }}
        .app-header {{
            font-size: 1.6rem;
            font-weight: 700;
            color: {PRIMARY if not dark_mode else '#e6edf3'};
            margin-bottom: 0.25rem;
        }}
        .app-subheader {{
            color: #64748b;
            margin-bottom: 1.5rem;
        }}
        .quick-synopsis {{
            font-size: 1rem;
            color: #475569;
            margin: 0.3rem 0 1rem 0;
            padding: 0.5rem 0.9rem;
            border-left: 3px solid {ACCENT};
            background-color: {ACCENT}0d;
            border-radius: 0 6px 6px 0;
        }}
        div[data-testid="stMetricValue"] {{
            font-size: 1.8rem;
        }}
    </style>
    """, unsafe_allow_html=True)


def status_badge_html(status):
    css_class = {"PASS": "status-pass", "FAIL": "status-fail", "INCOMPLETE": "status-incomplete"}.get(status, "")
    return f'<span class="status-badge {css_class}">{status}</span>'
