import streamlit as st
import pandas as pd
import numpy as np
from azure.storage.blob import BlobServiceClient
from io import StringIO, BytesIO
from datetime import datetime
import os
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.units import inch

# =========================================================
# SHEET SEARCH APPLICATION - November 2025
# =========================================================
# This app searches for sheet stock by width and length.
# Results include:
#
# 1. EXACT SHEET MATCHES:
#    - Sheets with BOTH width AND length matching exactly
#
# 2. ALTERNATIVE SHEETS:
#    - Sheets that are >= requested width AND >= requested length
#    - Waste calculated based on area difference
#
# 3. ROLLS (as alternatives):
#    - Rolls that are >= requested width (can be cut to length)
#    - Shown with splitting calculations and waste %
#
# All alternatives are filtered by max waste percentage.
# Users select from exact and alternative options to build quotes.
# =========================================================

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(page_title="Norkol Sheet Stock Search", page_icon="📦", layout="wide")

# Session state: selections & search params
if "sel_exact_idx" not in st.session_state:
    st.session_state.sel_exact_idx = set()
if "sel_alt_sheets_idx" not in st.session_state:
    st.session_state.sel_alt_sheets_idx = set()
if "sel_alt_rolls_idx" not in st.session_state:
    st.session_state.sel_alt_rolls_idx = set()
# Per-individual-roll selection within the detail expanders
if "sel_alt_sheets_detail" not in st.session_state:
    st.session_state.sel_alt_sheets_detail = set()
if "sel_alt_rolls_detail" not in st.session_state:
    st.session_state.sel_alt_rolls_detail = set()
# Tracks which aggregate groups have already had their rolls auto-populated
if "seen_alt_sheets_groups" not in st.session_state:
    st.session_state.seen_alt_sheets_groups = set()
if "seen_alt_rolls_groups" not in st.session_state:
    st.session_state.seen_alt_rolls_groups = set()
if "search_params" not in st.session_state:
    st.session_state.search_params = {}

# Secrets
try:
    # Try environment variable first (Azure App Service)
    AZURE_CONNECTION_STRING = os.environ.get("AZURE_CONNECTION_STRING")
    
    # Fall back to secrets (for local development)
    if not AZURE_CONNECTION_STRING:
        AZURE_CONNECTION_STRING = st.secrets["AZURE_CONNECTION_STRING"]
    
    if not AZURE_CONNECTION_STRING:
        raise KeyError("AZURE_CONNECTION_STRING")
        
except KeyError as e:
    st.error(f"Missing secret: {e}")
    st.stop()

CONTAINER_NAME = "data"
BLOB_NAME = "Inventory/Norkol_Inventory"
PAPER_INFO_BLOB = "PaperInformation.csv"
MACHINE_INFO_BLOB = "MachineInfo.csv"
GRADE_TABLE_BLOB = "Grade"
ORDER_SIZE_ADJ_BLOB = "Order Size Adjustments.csv"
PO_DETAIL_BLOB = "SODetail"
RESERVE_INV_BLOB = "Inventory/ReserveInventory.csv"

# Reserved rolls/sheets must be aged past this many days before they can be pulled
# into a quote scenario (also drives the read-only Reserved Inventory panel).
RESERVED_MIN_AGE_DAYS = 30

# Non-paper stock (plastic bags, tape, stretch wrap, barrier film, pouches) rides along
# in the same extract, but its QtyOnHand is a case/piece count rather than pounds — so
# InvValue / QtyOnHand * 100 produces a meaningless $/CWT. It is not quotable roll or
# sheet stock, so it is dropped at load rather than filtered per search.
EXCLUDED_PRODUCT_CATEGORIES = {"INDUSTRIAL PKG SUPPLIES", "INDUSTRIAL PACKAGING"}

# =========================================================
# DATA LOADING
# =========================================================
@st.cache_data(ttl=3600)
def load_inventory_data():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=BLOB_NAME)
        csv_content = blob_client.download_blob().readall().decode("utf-8")
        df = pd.read_csv(StringIO(csv_content), on_bad_lines="skip", encoding="utf-8")
        # 🔧 FIX: Ensure BasisWt is numeric
        if "BasisWt" in df.columns:
            df["BasisWt"] = pd.to_numeric(df["BasisWt"], errors="coerce")
        return df, datetime.now()
    except Exception as e:
        st.error(f"Error loading inventory: {str(e)}")
        return None, None


@st.cache_data(ttl=3600)
def load_supplementary_data():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)

        # Grade table (GradeID -> Area(IN), GSM, ProductGroupID)
        grade_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=GRADE_TABLE_BLOB)
        grade_csv = grade_client.download_blob().readall().decode("utf-8")
        grade_df = pd.read_csv(StringIO(grade_csv))
        grade_df.columns = grade_df.columns.str.strip()
        grade_df["GradeID"] = grade_df["GradeID"].astype(str).str.strip()
        grade_df["ProductGroupID"] = grade_df["ProductGroupID"].astype(str).str.strip()
        grade_df["GSM"] = pd.to_numeric(grade_df["GSM"], errors="coerce")
        grade_df["Area(IN)"] = pd.to_numeric(grade_df["Area(IN)"], errors="coerce")

        # PaperInformation (ProductGroupID + GSM_Factor -> RW_RunAdjust, SHT_RunAdjust, NumShtrRolls, etc.)
        paper_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=PAPER_INFO_BLOB)
        paper_csv = paper_client.download_blob().readall().decode("utf-8")
        paper_df = pd.read_csv(StringIO(paper_csv))
        paper_df.columns = paper_df.columns.str.strip()
        paper_df["ProductGroupID"] = paper_df["ProductGroupID"].astype(str).str.strip()
        paper_df["GSM_Factor"] = pd.to_numeric(paper_df["GSM_Factor"], errors="coerce")

        # MachineInfo
        machine_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=MACHINE_INFO_BLOB)
        machine_csv = machine_client.download_blob().readall().decode("utf-8")
        machine_df = pd.read_csv(StringIO(machine_csv))
        machine_df.columns = machine_df.columns.str.strip()

        return grade_df, paper_df, machine_df
    except Exception as e:
        st.warning(f"Could not load supplementary data: {str(e)}")
        return None, None, None


@st.cache_data(ttl=3600)
def load_order_size_adjustments():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=ORDER_SIZE_ADJ_BLOB)
        csv_content = blob_client.download_blob().readall().decode("utf-8")
        order_size_adj_df = pd.read_csv(StringIO(csv_content))
        order_size_adj_df.columns = order_size_adj_df.columns.str.strip()
        order_size_adj_df["Minimum"] = pd.to_numeric(order_size_adj_df["Minimum"], errors="coerce")
        order_size_adj_df["Maximum"] = pd.to_numeric(order_size_adj_df["Maximum"], errors="coerce")
        return order_size_adj_df
    except Exception as e:
        st.warning(f"Could not load order size adjustments: {str(e)}")
        return None


def drop_non_roll_stock(frame):
    """Drop non-paper categories whose QtyOnHand is a unit count, not pounds.

    Applied once at load so the search pool, the product-group dropdown and the
    reserved-inventory panel all agree on what counts as quotable stock.
    """
    if frame is None or frame.empty or "ProductCategoryID" not in frame.columns:
        return frame
    cat = frame["ProductCategoryID"].astype(str).str.strip().str.upper()
    return frame[~cat.isin(EXCLUDED_PRODUCT_CATEGORIES)].copy()


@st.cache_data(ttl=3600)
def load_reserve_inventory():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=RESERVE_INV_BLOB)
        csv_content = blob_client.download_blob().readall().decode("utf-8")
        ri_df = pd.read_csv(StringIO(csv_content), on_bad_lines="skip", encoding="utf-8")
        ri_df.columns = ri_df.columns.str.strip()
        # Strip thousands separators before numeric coercion (inv values may be "2,072.29")
        for col in ["InvValue", "InvVal", "InventoryValue", "Value"]:
            if col in ri_df.columns:
                ri_df[col] = ri_df[col].replace({',': ''}, regex=True)
        for col in ["BasisWt", "Caliper", "Roll_Width", "QtyOnHand", "Diameter", "Units",
                     "SheetWidth", "SheetLength", "InvValue", "InvVal", "InventoryValue", "Value"]:
            if col in ri_df.columns:
                ri_df[col] = pd.to_numeric(ri_df[col], errors="coerce")
        if "ReserveDate" in ri_df.columns:
            ri_df["ReserveDate"] = pd.to_datetime(ri_df["ReserveDate"], errors="coerce")
        return ri_df
    except Exception as e:
        st.warning(f"Could not load reserve inventory: {str(e)}")
        return None


@st.cache_data(ttl=3600)
def load_po_detail():
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=PO_DETAIL_BLOB)
        csv_content = blob_client.download_blob().readall().decode("utf-8")
        po_df = pd.read_csv(StringIO(csv_content), on_bad_lines="skip", encoding="utf-8")
        po_df.columns = po_df.columns.str.strip()
        for col in ["BasisWt", "Caliper", "Roll_Width", "Price", "PriceCWT", "WeightLB", "MWeight"]:
            if col in po_df.columns:
                po_df[col] = pd.to_numeric(po_df[col], errors="coerce")
        if "GradeID" in po_df.columns:
            po_df["GradeID"] = (
                pd.to_numeric(po_df["GradeID"], errors="coerce")
                .fillna(0).astype(int).astype(str).str.strip()
            )
        if "PODate" in po_df.columns:
            po_df["PODate"] = pd.to_datetime(po_df["PODate"], errors="coerce")
        return po_df
    except Exception as e:
        st.warning(f"Could not load PO detail data: {str(e)}")
        return None


# =========================================================
# ORDER SIZE ADJUSTMENT LOOKUP
# =========================================================
def get_order_size_pct(adj_df, process, description, order_qty):
    """Look up order size adjustment percentage. Returns decimal (e.g. 0.03 for 3%)."""
    if adj_df is None or order_qty is None:
        return 0.0

    process_map = {"Rewinder": "Rewinding", "Sheeter": "Sheeting"}
    process_name = process_map.get(process, process)

    matches = adj_df[
        (adj_df["MachineGroup"].str.strip() == process_name)
        & (adj_df["AdjDescription"].str.strip() == description)
        & (adj_df["Minimum"] <= order_qty)
        & (adj_df["Maximum"] >= order_qty)
    ]
    if matches.empty:
        return 0.0

    val = matches.iloc[0]["Adjustment"]
    if isinstance(val, str):
        val = val.strip().rstrip("%")
    try:
        return float(val) / 100.0
    except (ValueError, TypeError):
        return 0.0


# =========================================================
# SHEETS -> POUNDS
# =========================================================
def sheets_to_lbs(sheets, sheet_width, sheet_length, basis_wt_lbs, area_in):
    """
    Weight of N sheets: sheets * W * L * BasisWt(lbs) / (500 * Area(IN)).

    Equivalent to (sheets / 1000) * Mweight, where
    Mweight = ((W * L) / Area(IN)) * BasisWt(lbs) * 2.
    """
    try:
        if not (sheets and sheet_width and sheet_length and basis_wt_lbs and area_in):
            return None
        return (
            float(sheets) * float(sheet_width) * float(sheet_length) * float(basis_wt_lbs)
        ) / (500.0 * float(area_in))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def make_qty_lbs_fn(params):
    """
    Sheets-mode order quantity resolver.

    When the order is entered in sheets the pounds depend on basis weight, so a
    single order-quantity-in-lbs cannot be known before lots are picked. Returns
    a callable row -> lbs that derives the weight from that row's own BasisWt /
    BasisWtUOM, letting a search span multiple basis weights. Grade — and with it
    Area(IN) — is fixed for the search, so only basis weight varies.

    Returns None in LBS mode, or when the sheets inputs are incomplete, in which
    case callers fall back to the single order-quantity-in-lbs scalar.
    """
    if params.get("order_qty_unit") != "Sheets":
        return None

    sheets = params.get("order_quantity_sheets")
    area_in = params.get("sheets_area_in")
    width = params.get("sheets_width")
    length = params.get("sheets_length")
    gsm_factor = params.get("sheets_gsm_factor")

    if not (sheets and area_in and width and length):
        return None

    def _qty_lbs(row):
        basis_wt = pd.to_numeric(row.get("BasisWt"), errors="coerce")
        if pd.isna(basis_wt) or basis_wt <= 0:
            return None
        uom = str(row.get("BasisWtUOM", "LB") or "LB").strip().upper()
        if uom == "GSM":
            if not gsm_factor:
                return None
            basis_wt = basis_wt / gsm_factor
        return sheets_to_lbs(sheets, width, length, basis_wt, area_in)

    return _qty_lbs


# =========================================================
# CONVERTING COST CALCULATION
# =========================================================
def calculate_conversion_cost(row, requested_width, grade_df, paper_info_df, machine_info_df, order_quantity=None, order_size_adj_df=None):
    """
    Calculate converting cost metrics for a grouped alternative roll.
    Uses Yield (preferred) or QtyOnHand as processing weight.

    Data sources:
    - Grade table: GradeID -> Area(IN), GSM, ProductGroupID
    - PaperInformation: ProductGroupID + GSM_Factor -> RW_RunAdjust, SHT_RunAdjust, NumShtrRolls
    - MachineInfo: EquipType -> AvgSpeed, HourlyRate, Roll_Change_Hrs, Setup_Hrs

    NumShtrRolls logic:
    - If caliper > 0.011: NumShtrRolls = 1
    - Otherwise: NumShtrRolls from PaperInformation table

    Formula: Lbs/Hour = BasisWt/(Area*500) * (CutWidth * NumCuts * NumShtrRolls) * (AvgSpeed * 12) * 60

    Returns: LbsPerHour, ConvHrs, ConvertingCostPerCWT
    """
    try:
        grade_id = str(row.get("GradeID", "")).strip()
        grade_name = str(row.get("GradeName", "")).lower()
        equip_type = "Sheeter"

        # Lookup grade info (Area(IN), GSM, ProductGroupID) from Grade table
        grade_row = None
        if grade_df is not None and grade_id and grade_id != "nan":
            gr = grade_df[grade_df["GradeID"] == grade_id]
            if not gr.empty:
                grade_row = gr.iloc[0]

        # Lookup paper info (SHT_RunAdjust, NumShtrRolls) via ProductGroupID from Grade table
        paper_row = None
        if grade_row is not None and paper_info_df is not None:
            prod_group_id = str(grade_row["ProductGroupID"]).strip()
            pr = paper_info_df[
                paper_info_df["ProductGroupID"].astype(str).str.strip() == prod_group_id
            ]
            if not pr.empty:
                paper_row = pr.iloc[0]

        # Lookup machine info
        machine_row = None
        if machine_info_df is not None and "EquipType" in machine_info_df.columns:
            mr = machine_info_df[machine_info_df["EquipType"].astype(str).str.strip() == equip_type]
            machine_row = mr.iloc[0] if len(mr) else None

        if grade_row is None or paper_row is None or machine_row is None:
            return pd.Series(
                {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None}
            )

        # Inputs
        basis_wt = float(row.get("BasisWt", 0.0) or 0.0)
        basis_uom = row.get("BasisWtUOM", "LB")
        caliper = float(row.get("Caliper", 0.0) or 0.0)
        area_in = float(grade_row.get("Area(IN)", 0.0) or 0.0)
        avg_speed = float(
            machine_row.get(
                "AvgSpeed(FPM)",
                machine_row.get(
                    "avgspeed(FPM)",
                    machine_row.get("AvgSpeed", 2200.0),
                ),
            )
            or 2200.0
        )
        hourly_rate = float(machine_row.get("HourlyRate", 273.0) or 273.0)
        # Roll_Change_Hrs: explicit check so 0.0 is not treated as falsy
        rc_val = machine_row.get("Roll_Change_Hrs")
        roll_change_hrs = 0.25 if (rc_val is None or pd.isna(rc_val)) else float(rc_val)

        # Setup_hrs - try multiple column name variations
        setup_hrs = 0.0
        for col_name in ["Setup_hrs", "Setup_Hrs", "SetupHrs", "setup_hrs", "SETUP_HRS"]:
            if col_name in machine_row.index:
                setup_hrs = float(machine_row.get(col_name, 0.0) or 0.0)
                break

        splits = int(row.get("Splits", 1) or 1)  # NumCuts

        # Determine NumShtrRolls based on caliper
        # If caliper > 0.011 (board grade): NumShtrRolls = 1
        # Otherwise: lookup from PaperInformation (already joined via ProductGroupID + GSM)
        if caliper > 0.011:
            num_shtr_rolls = 1
        else:
            num_shtr_rolls = 1  # default
            num_shtr_rolls_val = paper_row.get("NumShtrRolls", None)
            if num_shtr_rolls_val is not None and pd.notna(num_shtr_rolls_val):
                try:
                    num_shtr_rolls = int(float(num_shtr_rolls_val))
                except (ValueError, TypeError):
                    num_shtr_rolls = 1

        # Ensure at least 1
        if num_shtr_rolls < 1:
            num_shtr_rolls = 1

        # SHT_RunAdjust: per-grade sheeting efficiency multiplier (default 1.0 if missing)
        sht_run_adjust_val = paper_row.get("SHT_RunAdjust", None)
        if sht_run_adjust_val is None or pd.isna(sht_run_adjust_val):
            sht_run_adjust = 1.0
        else:
            try:
                sht_run_adjust = float(sht_run_adjust_val)
            except (ValueError, TypeError):
                sht_run_adjust = 1.0

        # Basis weight to LB if needed (GSM from Grade table)
        if basis_uom == "GSM":
            gsm_factor = float(grade_row.get("GSM", 0.0) or 0.0)
            basis_lb = basis_wt / gsm_factor if gsm_factor else basis_wt
        else:
            basis_lb = basis_wt

        if not area_in or not avg_speed or requested_width is None:
            return pd.Series(
                {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None}
            )

        # Lbs/Hour = BasisWt/(Area*500) * (CutWidth * NumCuts * NumShtrRolls) * (AvgSpeed * 12) * 60 * SHT_RunAdjust
        lbs_per_hour = (
            (basis_lb / (area_in * 500.0))
            * (float(requested_width) * splits * num_shtr_rolls)
            * (avg_speed * 12.0)
            * 60.0
            * sht_run_adjust
        )

        # Processing weight: use Yield if available, else QtyOnHand
        process_weight = row.get("Yield", None)
        if process_weight is None or pd.isna(process_weight):
            process_weight = float(row.get("QtyOnHand", 0.0) or 0.0)

        # Number of rolls (for roll changes)
        units = row.get("Units", None)
        num_rolls = int(units) if units not in [None, 0] else 1

        processing_hours = (process_weight / lbs_per_hour) if (lbs_per_hour and lbs_per_hour > 0) else 0.0
        roll_change_hours = roll_change_hrs * num_rolls
        total_hours = processing_hours + roll_change_hours + setup_hrs  # Setup added once per group

        total_cost = total_hours * hourly_rate

        # Raw per-row CWT — as if only this alternative were used, no minimum
        # and no OrderQty surcharge. Both are applied at the summary level
        # once rows are selected.
        conv_cwt = (
            (total_cost / process_weight) * 100.0
            if process_weight and process_weight > 0
            else None
        )

        return pd.Series(
            {
                "LbsPerHour": lbs_per_hour,
                "ConvHrs": total_hours,
                "ConvertingCostPerCWT": conv_cwt,
            }
        )

    except Exception:
        return pd.Series(
            {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None}
        )


# =========================================================
# PDF REPORT GENERATION
# =========================================================
def generate_quote_pdf(search_params, selected_exact, selected_alt_sheets, selected_alt_rolls,
                        summary_data, detail_sheets=None, detail_rolls=None):
    """
    Generate a PDF quote report with parameters, selected lines, and summary.
    Returns PDF as bytes.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'], fontSize=18, spaceAfter=20)
    story.append(Paragraph("Norkol Sheet Stock Quote", title_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles['Normal']))
    story.append(Spacer(1, 20))
    
    # Search Parameters Section
    story.append(Paragraph("Search Parameters", styles['Heading2']))
    story.append(Spacer(1, 10))
    
    # Safely get parameter values
    basis_wt_list = search_params.get("basis_weights") or []
    caliper_list = search_params.get("calipers") or []
    grade_list = search_params.get("grade_names") or []

    # In sheets mode the pounds are resolved from the selected lots, so they arrive
    # via summary_data rather than the search params.
    _oq_sheets = search_params.get("order_quantity_sheets")
    _oq_lbs = search_params.get("order_quantity") or (summary_data or {}).get("order_qty")
    if search_params.get("order_qty_unit") == "Sheets" and _oq_sheets:
        _oq_text = f"{_oq_sheets:,} sheets" + (f" ({_oq_lbs:,.0f} lbs)" if _oq_lbs else "")
    elif _oq_lbs:
        _oq_text = f"{_oq_lbs:,.0f} lbs"
    else:
        _oq_text = "Not specified"


    param_data = [
        ["Warehouse Group:", str(search_params.get("warehouse_group") or "All")],
        ["Product Group:", str(search_params.get("product_group") or "All")],
        ["Grade:", ", ".join(str(g) for g in grade_list) if grade_list else "All"],
        ["Basis Weight:", ", ".join(str(b) for b in basis_wt_list) if basis_wt_list else "All"],
        ["Caliper:", ", ".join(str(c) for c in caliper_list) if caliper_list else "All"],
        ["Sheet Width:", f"{search_params.get('sheet_width_input')}\"" if search_params.get("sheet_width_input") else "Not specified"],
        ["Sheet Length:", f"{search_params.get('sheet_length_input')}\"" if search_params.get("sheet_length_input") else "Not specified"],
        ["Max Waste %:", f"{search_params.get('max_waste_pct')}%" if search_params.get("max_waste_pct") is not None else "Not specified"],
        ["Order Quantity:", _oq_text],
    ]
    
    param_table = Table(param_data, colWidths=[1.5*inch, 4*inch])
    param_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(param_table)
    story.append(Spacer(1, 20))
    
    # Selected Lines Section
    def add_lines_table(title, df, table_type="exact"):
        if df.empty:
            return
        story.append(Paragraph(title, styles['Heading2']))
        story.append(Spacer(1, 10))
        
        # Select columns to display based on table type
        if table_type == "exact":
            display_cols = ['GradeName', 'BasisWt', 'Caliper', 'Sheet_Width', 'Sheet_Length', 'Mill', 'QtyOnHand', 'AvgCost']
            headers = ['Grade', 'BasisWt', 'Caliper', 'Width', 'Length', 'Mill', 'Qty', 'AvgCost']
        elif table_type == "alt_sheets":
            display_cols = ['GradeName', 'BasisWt', 'Caliper', 'Sheet_Width', 'Sheet_Length', 'Mill', 'QtyOnHand', 'Yield', 'Waste_Pct', 'FinalCostCWT']
            headers = ['Grade', 'BasisWt', 'Caliper', 'Width', 'Length', 'Mill', 'Qty', 'Yield', 'Waste%', 'Final$/CWT']
        else:  # alt_rolls
            display_cols = ['GradeName', 'BasisWt', 'Caliper', 'Roll_Width', 'Mill', 'QtyOnHand', 'Yield', 'Splits', 'Waste_Pct', 'FinalCostCWT']
            headers = ['Grade', 'BasisWt', 'Caliper', 'Width', 'Mill', 'Qty', 'Yield', 'Splits', 'Waste%', 'Final$/CWT']
        
        # Filter to existing columns
        available_cols = [c for c in display_cols if c in df.columns]
        available_headers = [headers[display_cols.index(c)] for c in available_cols]
        
        # Build table data
        table_data = [available_headers]
        for _, row in df.iterrows():
            row_data = []
            for col in available_cols:
                val = row.get(col)
                try:
                    if val is None or pd.isna(val):
                        row_data.append('')
                    elif col in ['QtyOnHand', 'Yield']:
                        row_data.append(f"{float(val):,.0f}")
                    elif col in ['BasisWt']:
                        row_data.append(f"{float(val):.0f}")
                    elif col in ['Caliper']:
                        row_data.append(f"{float(val):.4f}")
                    elif col in ['Roll_Width', 'Sheet_Width', 'Sheet_Length']:
                        row_data.append(f"{float(val):.2f}")
                    elif col in ['Waste_Pct']:
                        row_data.append(f"{float(val):.1f}%")
                    elif col in ['AvgCost', 'FinalCostCWT']:
                        row_data.append(f"${float(val):.2f}")
                    elif col == 'Splits':
                        row_data.append(f"{int(val)}x")
                    elif col == 'Mill':
                        row_data.append(str(val)[:12] if val else '')  # Truncate supplier to 12 chars
                    elif col == 'GradeName':
                        row_data.append(str(val)[:15] if val else '')  # Truncate grade to 15 chars
                    else:
                        row_data.append(str(val)[:20] if val else '')  # Truncate other long strings
                except (ValueError, TypeError):
                    row_data.append('')
            table_data.append(row_data)
        
        # Create table with dynamic column widths
        col_width = 7.0 * inch / len(available_cols)
        lines_table = Table(table_data, colWidths=[col_width] * len(available_cols))
        lines_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(lines_table)
        story.append(Spacer(1, 20))
    
    add_lines_table("Exact Matches Selected", selected_exact, table_type="exact")
    add_lines_table("Alternative Sheets Selected", selected_alt_sheets, table_type="alt_sheets")
    add_lines_table("Alternative Rolls Selected", selected_alt_rolls, table_type="alt_rolls")

    # ---- Individual rolls picked in the detail expanders ----
    def add_rolls_detail_table(title, df, kind="rolls"):
        if df is None or df.empty:
            return
        if kind == "sheets":
            cols = ["LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "SheetWidth", "SheetLength", "Mill", "QtyOnHand", "CostPerCWT"]
            headers = ["Lot", "Roll#", "Grade", "BWt", "Cal",
                       "ShW", "ShL", "Mill", "Qty", "$/CWT"]
        else:
            cols = ["LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "Roll_Width", "Diameter", "Mill", "QtyOnHand", "CostPerCWT"]
            headers = ["Lot", "Roll#", "Grade", "BWt", "Cal",
                       "Width", "Dia", "Mill", "Qty", "$/CWT"]

        available = [c for c in cols if c in df.columns]
        if not available:
            return
        available_headers = [headers[cols.index(c)] for c in available]

        story.append(Paragraph(title, styles['Heading3']))
        story.append(Spacer(1, 6))

        table_data = [available_headers]
        for _, row in df.iterrows():
            row_data = []
            for col in available:
                val = row.get(col)
                try:
                    if val is None or pd.isna(val):
                        row_data.append('')
                    elif col == "QtyOnHand":
                        row_data.append(f"{float(val):,.0f}")
                    elif col == "BasisWt":
                        row_data.append(f"{float(val):.0f}")
                    elif col == "Caliper":
                        row_data.append(f"{float(val):.4f}")
                    elif col in ("Roll_Width", "SheetWidth", "SheetLength"):
                        row_data.append(f"{float(val):.2f}")
                    elif col == "Diameter":
                        row_data.append(f"{float(val):.0f}")
                    elif col == "CostPerCWT":
                        row_data.append(f"${float(val):.2f}")
                    elif col == "GradeName":
                        row_data.append(str(val)[:12] if val else '')
                    elif col == "Mill":
                        row_data.append(str(val)[:10] if val else '')
                    else:
                        row_data.append(str(val)[:10] if val else '')
                except (ValueError, TypeError):
                    row_data.append('')
            table_data.append(row_data)

        col_width = 7.3 * inch / len(available)
        detail_table = Table(table_data, colWidths=[col_width] * len(available), repeatRows=1)
        detail_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.darkgrey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 7),
            ('FONTSIZE', (0, 1), (-1, -1), 6.5),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ]))
        story.append(detail_table)
        story.append(Spacer(1, 14))

    # Summary Section
    story.append(Paragraph("Quote Summary", styles['Heading2']))
    story.append(Spacer(1, 10))
    
    exact_lbs = summary_data.get('exact_lbs') or 0
    alt_yield = summary_data.get('alt_yield') or 0
    total_lbs = summary_data.get('total_lbs') or 0
    mweight = summary_data.get('mweight')
    blended_cwt = summary_data.get('blended_cwt') or 0
    cost_per_m = summary_data.get('cost_per_m')
    est_sheets = summary_data.get('est_sheets')
    order_qty = summary_data.get('order_qty')
    order_qty_cost_cwt = summary_data.get('order_qty_cost_cwt')
    order_qty_cost_per_m = summary_data.get('order_qty_cost_per_m')
    freight_total = summary_data.get('freight_total')
    freight_cwt = summary_data.get('freight_cwt')
    freight_per_m = summary_data.get('freight_per_m')

    summary_table_data = [
        ["Exact Qty Selected:", f"{exact_lbs:,.0f} lbs"],
        ["Alt Yield Selected:", f"{alt_yield:,.0f} lbs"],
        ["Total Estimated Yield:", f"{total_lbs:,.0f} lbs"],
        ["Mweight:", f"{mweight:,.0f} lbs" if mweight else "—"],
        ["Blended Cost / CWT:", f"${blended_cwt:,.2f}"],
        ["Cost Per M Sheets:", f"${cost_per_m:,.2f}" if cost_per_m else "—"],
        ["Estimated Sheets:", f"{est_sheets:,.0f}" if est_sheets else "—"],
        ["Order Qty:", f"{order_qty:,.0f} lbs" if order_qty else "—"],
    ]
    if freight_total:
        summary_table_data.extend([
            ["Freight Total:", f"${freight_total:,.2f}"],
            ["Freight / CWT:", f"${freight_cwt:,.2f}" if freight_cwt else "—"],
            ["Freight / M Sheets:", f"${freight_per_m:,.2f}" if freight_per_m else "—"],
        ])
    summary_table_data.extend([
        ["Order Qty Cost / CWT:", f"${order_qty_cost_cwt:,.2f}" if order_qty_cost_cwt is not None else "—"],
        ["Order Qty Cost / M Sheets:", f"${order_qty_cost_per_m:,.2f}" if order_qty_cost_per_m is not None else "—"],
    ])
    
    summary_table = Table(summary_table_data, colWidths=[2*inch, 2*inch])
    summary_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    story.append(summary_table)

    # Per-roll detail (potentially lengthy) — push to a new page so the summary stays on page 1
    has_detail = (
        (detail_sheets is not None and not detail_sheets.empty)
        or (detail_rolls is not None and not detail_rolls.empty)
    )
    if has_detail:
        story.append(PageBreak())
        add_rolls_detail_table("Selected Alt-Sheet Rolls (detail)", detail_sheets, kind="sheets")
        add_rolls_detail_table("Selected Alt Rolls (detail)", detail_rolls, kind="rolls")

    # Build PDF
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# =========================================================
# LOAD DATA
# =========================================================
df, last_refresh = load_inventory_data()
grade_df, paper_info_df, machine_info_df = load_supplementary_data()
order_size_adj_df = load_order_size_adjustments()
po_detail_df = load_po_detail()
reserve_inv_df = load_reserve_inventory()
if df is None:
    st.stop()

# Keep non-paper items out of both candidate pools before anything reads them.
df = drop_non_roll_stock(df)
reserve_inv_df = drop_non_roll_stock(reserve_inv_df)

# =========================================================
# SIDEBAR
# =========================================================
with st.sidebar:
    st.title("📦 Norkol Sheet Stock Search")
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.session_state.sel_exact_idx = set()
        st.session_state.sel_alt_sheets_idx = set()
        st.session_state.sel_alt_rolls_idx = set()
        st.session_state.sel_alt_sheets_detail = set()
        st.session_state.sel_alt_rolls_detail = set()
        st.session_state.seen_alt_sheets_groups = set()
        st.session_state.seen_alt_rolls_groups = set()
        st.session_state.search_params = {}
        st.rerun()
    if last_refresh:
        st.info(f"Updated: {last_refresh.strftime('%I:%M %p')}")
    st.metric("Total Items", f"{len(df):,}")
    st.success("✅ Grade Table Loaded" if grade_df is not None else "⚠️ Grade Table Missing")
    st.success("✅ Paper Info Loaded" if paper_info_df is not None else "⚠️ Paper Info Missing")
    st.success("✅ Machine Info Loaded" if machine_info_df is not None else "⚠️ Machine Info Missing")
    st.success("✅ Order Size Adj Loaded" if order_size_adj_df is not None else "⚠️ Order Size Adj Missing")
    st.success("✅ PO Detail Loaded" if po_detail_df is not None else "⚠️ PO Detail Missing")

# =========================================================
# MAIN TITLE
# =========================================================
st.title("🔍 Norkol Sheet Stock Search")

# =========================================================
# SEARCH FORM
# =========================================================
with st.container():
    col1, col2 = st.columns(2)

    with col1:
        rc = st.session_state.get("reset_counter", 0)
        wh_opts = (
            ["All"] + sorted(df["WarehouseGroup"].dropna().unique().tolist())
            if "WarehouseGroup" in df.columns
            else ["All"]
        )
        warehouse_group = st.selectbox("Warehouse Group", wh_opts, key=f"fld_warehouse_group_{rc}")

        if "ProductGroupID" in df.columns:
            pg_opts = ["All"] + sorted(df["ProductGroupID"].dropna().unique().tolist())
            product_group = st.selectbox("Product Group", pg_opts, key=f"fld_product_group_{rc}")
        elif "ProductGroup" in df.columns:
            pg_opts = ["All"] + sorted(df["ProductGroup"].dropna().unique().tolist())
            product_group = st.selectbox("Product Group", pg_opts, key=f"fld_product_group_{rc}")
        else:
            product_group = "All"

        gn_opts = (
            sorted(df["GradeName"].dropna().unique().tolist())
            if "GradeName" in df.columns
            else []
        )
        grade_names = st.multiselect("Grade Name(s)", gn_opts, placeholder="All grades (leave empty)", key=f"fld_grade_names_{rc}")

        bw_opts = (
            sorted([x for x in df["BasisWt"].dropna().unique().tolist()])
            if "BasisWt" in df.columns
            else []
        )
        basis_weights = st.multiselect("Basis Weight(s)", bw_opts, placeholder="All weights (leave empty)", key=f"fld_basis_weights_{rc}")
        basis_wt_unit = st.radio(
            "Basis Weight Unit",
            options=["LBS", "GSM"],
            horizontal=True,
            help="Unit of the basis weight you entered. GSM values are converted to LBS using the grade's GSM factor before MWeight / order-weight calculations.",
            key=f"fld_basis_wt_unit_{rc}",
        )

        if "Caliper" in df.columns:
            caliper_values = pd.to_numeric(df["Caliper"], errors="coerce").dropna().unique()
            cal_opts = [f"{x:.4f}" for x in sorted(caliper_values)]
            calipers = st.multiselect("Caliper(s)", cal_opts, placeholder="All calipers (leave empty)", key=f"fld_calipers_{rc}")
        else:
            calipers = []

    with col2:
        sheet_width_input = st.text_input("Sheet Width Needed", placeholder='e.g., 48 or 48.5', key=f"fld_sheet_width_{rc}")
        sheet_length_input = st.text_input("Sheet Length Needed", placeholder='e.g., 36 or 36.5', key=f"fld_sheet_length_{rc}")
        max_waste_pct = st.number_input(
            "Max Waste % (for alternatives)",
            min_value=0.0,
            max_value=100.0,
            value=10.0,
            step=1.0,
            key=f"fld_max_waste_pct_{rc}",
        )
        order_qty_unit = st.radio(
            "Order Quantity Unit",
            options=["LBS", "Sheets"],
            horizontal=True,
            key=f"fld_order_qty_unit_{rc}",
        )
        qty_label = "Order Quantity (sheets) *" if order_qty_unit == "Sheets" else "Order Quantity (lbs) *"
        order_quantity = st.number_input(
            qty_label,
            min_value=0,
            value=0,
            key=f"fld_order_quantity_{rc}",
        )
        if order_qty_unit == "Sheets":
            st.caption(
                "Order quantity in sheets requires exactly one Grade Name, plus Sheet Width "
                "and Sheet Length. Multiple basis weights are allowed — the order weight is "
                "resolved from the lots you select."
            )

            preview_missing = []
            if len(grade_names) != 1:
                preview_missing.append("one Grade Name")
            try:
                _w_pv = float(str(sheet_width_input).strip()) if sheet_width_input else None
            except ValueError:
                _w_pv = None
            try:
                _l_pv = float(str(sheet_length_input).strip()) if sheet_length_input else None
            except ValueError:
                _l_pv = None
            if not _w_pv:
                preview_missing.append("Sheet Width")
            if not _l_pv:
                preview_missing.append("Sheet Length")

            _area_pv = None
            _gsm_factor_pv = None
            if len(grade_names) == 1 and grade_df is not None and "Description" in grade_df.columns:
                _gm = grade_df[grade_df["Description"].astype(str).str.strip() == str(grade_names[0]).strip()]
                if not _gm.empty:
                    _av = _gm.iloc[0].get("Area(IN)")
                    if pd.notna(_av) and float(_av) > 0:
                        _area_pv = float(_av)
                    _gv = _gm.iloc[0].get("GSM")
                    if pd.notna(_gv) and float(_gv) > 0:
                        _gsm_factor_pv = float(_gv)

            # One MWeight / lbs estimate per basis weight the user has filtered on.
            # The radio is what the typed filter values mean; the actual results use
            # each inventory row's own BasisWtUOM.
            preview_rows = []
            if not preview_missing and order_quantity and order_quantity > 0 and _area_pv:
                for _bw_raw in basis_weights:
                    try:
                        _bw_pv = float(_bw_raw)
                    except (TypeError, ValueError):
                        continue
                    if basis_wt_unit == "GSM":
                        if not _gsm_factor_pv:
                            continue
                        _bw_pv = _bw_pv / _gsm_factor_pv
                    _lbs_pv = sheets_to_lbs(int(order_quantity), _w_pv, _l_pv, _bw_pv, _area_pv)
                    if _lbs_pv is None:
                        continue
                    preview_rows.append({
                        "Basis Wt": _bw_raw,
                        "MWeight": round((_lbs_pv / int(order_quantity)) * 1000.0, 1),
                        "Est. lbs": round(_lbs_pv),
                    })

            if basis_wt_unit == "GSM" and len(grade_names) == 1 and _gsm_factor_pv is None:
                st.caption("⚠️ GSM mode: no GSM factor found for the selected grade — MWeight/lbs preview disabled.")

            if len(preview_rows) == 1:
                pc1, pc2 = st.columns(2)
                with pc1:
                    st.metric("MWeight (lbs/1000 sheets)", f"{preview_rows[0]['MWeight']:,.1f}")
                with pc2:
                    st.metric("Estimated lbs", f"{preview_rows[0]['Est. lbs']:,.0f}")
            elif len(preview_rows) > 1:
                _lo = min(r["Est. lbs"] for r in preview_rows)
                _hi = max(r["Est. lbs"] for r in preview_rows)
                st.caption(f"{int(order_quantity):,} sheets ≈ {_lo:,.0f}–{_hi:,.0f} lbs across the selected basis weights:")
                st.dataframe(
                    pd.DataFrame(preview_rows),
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "MWeight": st.column_config.NumberColumn("MWeight", format="%.1f"),
                        "Est. lbs": st.column_config.NumberColumn("Est. lbs", format="%d"),
                    },
                )
            elif not preview_missing and order_quantity and order_quantity > 0 and _area_pv:
                st.caption("No basis weight filter — MWeight will be resolved from the lots you select.")

        freight_cost = st.number_input(
            "Freight Cost (total $, optional)",
            min_value=0.0,
            value=0.0,
            step=10.0,
            format="%.2f",
            help="Total freight expense for the order. Added to summary cost as $/CWT and $/M sheets.",
            key=f"fld_freight_cost_{rc}",
        )

        include_reserved = st.checkbox(
            f"🔒 Include reserved inventory (reserved > {RESERVED_MIN_AGE_DAYS} days)",
            help=(
                "Scenario mode: allows stock already reserved against a sales order to be "
                "quoted. Reserved rolls/sheets are flagged with 🔒 and their customer in the "
                "results. Releasing them still has to be cleared with the owning sales rep."
            ),
            key=f"fld_include_reserved_{rc}",
        )

    c1, c2 = st.columns([1, 3])
    with c1:
        search_btn = st.button("🔍 Search", use_container_width=True)
    with c2:
        reset_btn = st.button("🔄 Reset", use_container_width=True)

if reset_btn:
    for k in list(st.session_state.keys()):
        if k.startswith("fld_"):
            del st.session_state[k]
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_sheets_idx = set()
    st.session_state.sel_alt_rolls_idx = set()
    st.session_state.sel_alt_sheets_detail = set()
    st.session_state.sel_alt_rolls_detail = set()
    st.session_state.seen_alt_sheets_groups = set()
    st.session_state.seen_alt_rolls_groups = set()
    st.session_state.search_params = {}
    st.session_state.reset_counter = st.session_state.get("reset_counter", 0) + 1
    st.rerun()


# =========================================================
# AGGREGATION HELPERS
# =========================================================
# Columns identifying the reservation a roll/sheet is held against.
RESERVE_ID_COLS = ["ResSONum", "ReserveSalesRep", "ResCust"]

# The reserve extract uses several spellings of "no value" — notably a literal
# "(None)" in ResSONum. Most reserved stock is customer-level holds with no SO#.
_PLACEHOLDERS = {"", "nan", "none", "(none)", "null", "n/a", "-"}


def _with_reserved(group_cols, frame):
    """Append IsReserved to a section's group keys so reserved and free stock in the
    same grade/size never merge into one row. No-op when reserved stock isn't present."""
    if "IsReserved" in frame.columns and "IsReserved" not in group_cols:
        return group_cols + ["IsReserved"]
    return group_cols


def _clean_val(v):
    s = str(v).strip()
    return "" if s.lower() in _PLACEHOLDERS else s


def _join_uniq(s):
    """Collapse a group's reservation values into one display string."""
    vals = sorted({c for c in (_clean_val(v) for v in s.dropna()) if c})
    if not vals:
        return ""
    return ", ".join(vals[:3]) + ("…" if len(vals) > 3 else "")


def _reserve_agg(frame, agg_dict):
    """Add reservation columns to an aggregation spec, collapsed to display strings."""
    for c in RESERVE_ID_COLS:
        if c in frame.columns:
            agg_dict[c] = _join_uniq
    return agg_dict


def _reserved_label(row):
    """Short '🔒 customer (SO#)' marker for a grouped row, or '—' if free stock."""
    if not bool(row.get("IsReserved", False)):
        return "—"
    cust = _clean_val(row.get("ResCust", ""))
    so = _clean_val(row.get("ResSONum", ""))
    if cust and so:
        return f"🔒 {cust} (SO# {so})"
    if cust:
        return f"🔒 {cust}"
    if so:
        return f"🔒 SO# {so}"
    return "🔒 reserved"


def _detect_inv_col(df):
    for c in ["InvValue", "InvVal", "InventoryValue", "Value"]:
        if c in df.columns:
            return c
    return None


def _apply_run_waste(out, order_quantity, order_size_adj_df, qty_lbs_fn):
    """
    Apply the RunWaste bracket to an aggregated frame.

    LBS mode uses a single bracket for the whole job. Sheets mode resolves the
    bracket per group row from that group's own basis weight — group keys already
    include BasisWt, and a heavier sheet is genuinely more pounds for the same
    sheet count, so the per-group bracket is the more accurate of the two.
    """
    if out is None or out.empty or order_size_adj_df is None:
        return out

    if qty_lbs_fn is not None:
        oq_lbs = out.apply(qty_lbs_fn, axis=1)
        out["OrderQtyLbs"] = oq_lbs
        run_waste = oq_lbs.map(
            lambda q: get_order_size_pct(order_size_adj_df, "Sheeter", "RunWaste", q)
            if q
            else 0.0
        )
    elif order_quantity is not None:
        run_waste = pd.Series(
            get_order_size_pct(order_size_adj_df, "Sheeter", "RunWaste", order_quantity),
            index=out.index,
        )
    else:
        return out

    out["RunWastePct"] = run_waste
    if "Yield" in out.columns:
        out["Yield"] = out["Yield"] * (1 - run_waste)
    if "NetAvgCost" in out.columns:
        out["NetAvgCost"] = out["NetAvgCost"] * (1 + run_waste)
    return out


def aggregate_alt_sheets(al_sh, order_quantity, order_size_adj_df, machine_info_df,
                          qty_lbs_fn=None):
    """Aggregate raw alt-sheet rows into group rows with derived metrics."""
    if al_sh is None or al_sh.empty:
        return pd.DataFrame()

    al_sh = al_sh.copy()
    inv_col = _detect_inv_col(al_sh)

    width_col = next((c for c in ["SheetWidth", "Sheet_Width", "Width"] if c in al_sh.columns), None)
    length_col = next((c for c in ["SheetLength", "Sheet_Length", "Length"] if c in al_sh.columns), None)

    if "Caliper" in al_sh.columns:
        al_sh["Caliper"] = pd.to_numeric(al_sh["Caliper"], errors="coerce")

    if inv_col and inv_col in al_sh.columns:
        al_sh[inv_col] = al_sh[inv_col].replace({',': ''}, regex=True)
        al_sh[inv_col] = pd.to_numeric(al_sh[inv_col], errors="coerce")

    # Yield = QtyOnHand * (1 - Waste_Pct/100) — recompute per-row to stay consistent
    if "QtyOnHand" in al_sh.columns and "Waste_Pct" in al_sh.columns:
        al_sh["Yield"] = al_sh["QtyOnHand"] * (1 - al_sh["Waste_Pct"] / 100.0)

    group_cols = ["GradeName", "BasisWt", "Caliper", width_col, length_col, "Mill", "Brand"]
    group_cols = [c for c in group_cols if c and c in al_sh.columns]
    group_cols = _with_reserved(group_cols, al_sh)

    agg = {"QtyOnHand": "sum", "Yield": "sum", "Splits": "first", "Waste_Pct": "first"}
    if "Units" in al_sh.columns:
        agg["Units"] = "sum"
    if inv_col and inv_col in al_sh.columns:
        agg[inv_col] = "sum"
    if "GradeID" in al_sh.columns:
        agg["GradeID"] = "first"
    if "BasisWtUOM" in al_sh.columns:
        agg["BasisWtUOM"] = "first"
    _reserve_agg(al_sh, agg)

    out = al_sh.groupby(group_cols, as_index=False, dropna=False).agg(agg)

    if inv_col and inv_col in out.columns and "Yield" in out.columns:
        out["NetAvgCost"] = (out[inv_col] / out["Yield"]) * 100.0
        if "QtyOnHand" in out.columns:
            out["AvgCost"] = (out[inv_col] / out["QtyOnHand"]) * 100.0
    else:
        out["AvgCost"] = np.nan
        out["NetAvgCost"] = np.nan

    out = _apply_run_waste(out, order_quantity, order_size_adj_df, qty_lbs_fn)

    # Trimmer converting cost
    per_cwt_rate = None
    if machine_info_df is not None and "EquipType" in machine_info_df.columns:
        trimmer_row = machine_info_df[machine_info_df["EquipType"].astype(str).str.strip() == "Trimmer"]
        if len(trimmer_row) > 0:
            raw = trimmer_row.iloc[0].get("PerCWTRate", None)
            if raw is not None:
                if isinstance(raw, str):
                    raw = raw.replace('$', '').replace(',', '').strip()
                try:
                    per_cwt_rate = float(raw)
                except (ValueError, TypeError):
                    per_cwt_rate = None
    if per_cwt_rate is not None:
        out["ConvertingCostPerCWT"] = per_cwt_rate
        if "NetAvgCost" in out.columns:
            out["FinalCostCWT"] = out["NetAvgCost"].fillna(0.0) + per_cwt_rate
    else:
        out["ConvertingCostPerCWT"] = np.nan
        out["FinalCostCWT"] = np.nan

    if inv_col and inv_col in out.columns:
        out = out.drop(columns=[inv_col], errors="ignore")

    return out


def aggregate_alt_rolls(al_rl, requested_width, order_quantity, order_size_adj_df,
                         grade_df, paper_info_df, machine_info_df, qty_lbs_fn=None):
    """Aggregate raw roll rows into group rows with derived metrics."""
    if al_rl is None or al_rl.empty:
        return pd.DataFrame()

    al_rl = al_rl.copy()
    inv_col = _detect_inv_col(al_rl)

    if "Caliper" in al_rl.columns:
        al_rl["Caliper"] = pd.to_numeric(al_rl["Caliper"], errors="coerce")

    if inv_col and inv_col in al_rl.columns:
        al_rl[inv_col] = al_rl[inv_col].replace({',': ''}, regex=True)
        al_rl[inv_col] = pd.to_numeric(al_rl[inv_col], errors="coerce")

    if "QtyOnHand" in al_rl.columns and "Waste_Pct" in al_rl.columns:
        al_rl["Yield"] = al_rl["QtyOnHand"] * (1 - al_rl["Waste_Pct"] / 100.0)

    group_cols = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "Mill", "Brand"]
    group_cols = [c for c in group_cols if c in al_rl.columns]
    group_cols = _with_reserved(group_cols, al_rl)

    agg = {"QtyOnHand": "sum", "Yield": "sum", "Splits": "first", "Waste_Pct": "first"}
    if "Units" in al_rl.columns:
        agg["Units"] = "sum"
    if inv_col and inv_col in al_rl.columns:
        agg[inv_col] = "sum"
    if "GradeID" in al_rl.columns:
        agg["GradeID"] = "first"
    if "BasisWtUOM" in al_rl.columns:
        agg["BasisWtUOM"] = "first"
    if "ProductCategoryID" in al_rl.columns:
        agg["ProductCategoryID"] = "first"
    _reserve_agg(al_rl, agg)

    out = al_rl.groupby(group_cols, as_index=False, dropna=False).agg(agg)

    if inv_col and inv_col in out.columns and "Yield" in out.columns:
        out["NetAvgCost"] = (out[inv_col] / out["Yield"]) * 100.0
        if "QtyOnHand" in out.columns:
            out["AvgCost"] = (out[inv_col] / out["QtyOnHand"]) * 100.0
    else:
        out["AvgCost"] = np.nan
        out["NetAvgCost"] = np.nan

    out = _apply_run_waste(out, order_quantity, order_size_adj_df, qty_lbs_fn)

    if (
        not out.empty
        and grade_df is not None
        and paper_info_df is not None
        and machine_info_df is not None
    ):
        conv_series = out.apply(
            lambda r: calculate_conversion_cost(
                r, requested_width, grade_df, paper_info_df, machine_info_df,
                order_quantity=order_quantity, order_size_adj_df=order_size_adj_df
            ),
            axis=1,
        )
        if isinstance(conv_series, pd.DataFrame):
            conv_final = conv_series.reset_index(drop=True)
        else:
            conv_final = pd.DataFrame(list(conv_series)).reset_index(drop=True)
        out = pd.concat([out.reset_index(drop=True), conv_final], axis=1)

    if "NetAvgCost" in out.columns and "ConvertingCostPerCWT" in out.columns:
        out["FinalCostCWT"] = (
            out["NetAvgCost"].fillna(0.0) + out["ConvertingCostPerCWT"].fillna(0.0)
        )

    if inv_col and inv_col in out.columns:
        out = out.drop(columns=[inv_col], errors="ignore")

    return out


def _group_key_series(df, keys):
    """Return a Series of group-key tuples for matching raw rows to aggregate groups."""
    if df is None or df.empty:
        return pd.Series(dtype=object)
    return df[keys].apply(tuple, axis=1)


def sync_detail_selection(aggregated, raw, sel_agg_idx, group_keys,
                          detail_state_key, seen_state_key):
    """Reconcile per-roll detail selection with current aggregate-group selection.

    When an aggregate group is newly selected, all its underlying raw rolls are
    added to the detail-selection set. When it's de-selected, those rolls are
    dropped. Re-checking a previously-unchecked group restores all of its rolls.
    """
    if aggregated is None or aggregated.empty or raw is None or raw.empty:
        st.session_state[detail_state_key] = set()
        st.session_state[seen_state_key] = set()
        return

    available_keys = [k for k in group_keys if k in raw.columns and k in aggregated.columns]
    if not available_keys or "_detail_id" not in raw.columns:
        return

    current_groups = set()
    for idx in sel_agg_idx:
        if 0 <= idx < len(aggregated):
            row = aggregated.iloc[idx]
            current_groups.add(tuple(row.get(k) for k in available_keys))

    seen_groups = st.session_state.get(seen_state_key, set())
    newly_added = current_groups - seen_groups
    removed = seen_groups - current_groups

    detail_sel = set(st.session_state.get(detail_state_key, set()))

    if newly_added or removed:
        raw_keys = _group_key_series(raw, available_keys)
        if newly_added:
            for gk in newly_added:
                ids = raw.loc[raw_keys == gk, "_detail_id"].astype(int).tolist()
                detail_sel.update(ids)
        if removed:
            for gk in removed:
                ids = set(raw.loc[raw_keys == gk, "_detail_id"].astype(int).tolist())
                detail_sel.difference_update(ids)
        st.session_state[detail_state_key] = detail_sel
        st.session_state[seen_state_key] = current_groups


# =========================================================
# SEARCH FUNCTION (Unified)
# =========================================================
def run_search(params):
    warehouse_group = params.get("warehouse_group")
    product_group = params.get("product_group")
    grade_names = params.get("grade_names", [])
    basis_weights = params.get("basis_weights", [])
    calipers = params.get("calipers", [])
    sheet_width_input = params.get("sheet_width_input")
    sheet_length_input = params.get("sheet_length_input")
    max_waste_pct = params.get("max_waste_pct")
    order_quantity = params.get("order_quantity")
    include_reserved = params.get("include_reserved", False)

    # Sheets mode has no single order-quantity-in-lbs until lots are selected;
    # it carries a per-basis-weight resolver instead.
    qty_lbs_fn = make_qty_lbs_fn(params)

    if not order_quantity and qty_lbs_fn is None:
        st.error("Order quantity must be provided")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, pd.DataFrame(), pd.DataFrame()

    filtered = df.copy()

    # Scenario mode: fold aged reserved stock into the candidate pool. The reserve blob
    # carries the same schema as main inventory (plus the Res* columns) and the two are
    # disjoint, so a plain concat cannot double-count a roll/sheet. When the option is off
    # we add no columns at all, leaving the normal search path untouched.
    if include_reserved and reserve_inv_df is not None and not reserve_inv_df.empty:
        ri_pool = reserve_inv_df.copy()
        if "ReserveDate" in ri_pool.columns:
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=RESERVED_MIN_AGE_DAYS)
            ri_pool = ri_pool[ri_pool["ReserveDate"] <= cutoff]
        if not ri_pool.empty:
            filtered["IsReserved"] = False
            ri_pool["IsReserved"] = True
            filtered = pd.concat([filtered, ri_pool], ignore_index=True)
            filtered["IsReserved"] = filtered["IsReserved"].fillna(False).astype(bool)

            # Reservation columns must be blank rather than NaN on the free-stock rows:
            # they are carried through aggregation, and NaN keys interact badly with groupby.
            for _c in RESERVE_ID_COLS:
                if _c not in filtered.columns:
                    filtered[_c] = ""
                else:
                    filtered[_c] = filtered[_c].fillna("")

    # Filters
    if "WarehouseGroup" in filtered.columns and warehouse_group != "All":
        filtered = filtered[filtered["WarehouseGroup"] == warehouse_group]

    if product_group != "All":
        if "ProductGroupID" in filtered.columns:
            filtered = filtered[filtered["ProductGroupID"] == product_group]
        elif "ProductGroup" in filtered.columns:
            filtered = filtered[filtered["ProductGroup"] == product_group]

    # Multi-select filters: only filter if list is not empty
    if "GradeName" in filtered.columns and grade_names:
        filtered = filtered[filtered["GradeName"].isin(grade_names)]

    if "BasisWt" in filtered.columns and basis_weights:
        filtered = filtered[filtered["BasisWt"].isin(basis_weights)]

    if "Caliper" in filtered.columns and calipers:
        # Convert caliper strings back to floats for comparison
        caliper_floats = [float(c) for c in calipers]
        filtered = filtered[
            pd.to_numeric(filtered["Caliper"], errors="coerce").round(4).isin(caliper_floats)
        ]

    exact_matches = pd.DataFrame()
    alternative_rolls = pd.DataFrame()
    requested_width = None
    requested_length = None

    # Both width and length are required for search
    if not sheet_width_input or not sheet_length_input:
        st.warning("⚠️ Please enter both Sheet Width and Sheet Length to search")
        return exact_matches, pd.DataFrame(), alternative_rolls, None, pd.DataFrame(), pd.DataFrame()

    # Parse sheet dimensions
    try:
        requested_width = float(str(sheet_width_input).strip())
        requested_length = float(str(sheet_length_input).strip())
    except ValueError:
        st.error("❌ Please enter valid numbers for Sheet Width and Length")
        return exact_matches, pd.DataFrame(), alternative_rolls, None, pd.DataFrame(), pd.DataFrame()

    # Inventory value column
    inv_col = None
    for c in ["InvValue", "InvVal", "InventoryValue", "Value"]:
        if c in filtered.columns:
            inv_col = c
            break

    # =========================================================
    # SEARCH FOR SHEETS (width x length products)
    # =========================================================
    exact_sheets = pd.DataFrame()
    alt_sheets = pd.DataFrame()
    
    # Check if sheet dimensions exist in the data - try multiple variations
    # Create a lowercase column map for case-insensitive matching
    col_map = {col.lower().replace('_', '').replace(' ', ''): col for col in filtered.columns}
    
    # Try to find width column
    width_col = None
    for variant in ['sheetwidth', 'sheet_width', 'width']:
        if variant in col_map:
            width_col = col_map[variant]
            break
    
    # Try to find length column
    length_col = None
    for variant in ['sheetlength', 'sheet_length', 'length']:
        if variant in col_map:
            length_col = col_map[variant]
            break
    
    has_sheet_width = width_col is not None
    has_sheet_length = length_col is not None
    
    # Debug output to help identify issues
    if not has_sheet_width or not has_sheet_length:
        st.warning(f"⚠️ Sheet columns not found. Available columns: {', '.join(filtered.columns[:20])}")
        if not has_sheet_width:
            st.info("❌ Sheet width column not found. Looking for: SheetWidth, Sheet_Width, or Width")
        if not has_sheet_length:
            st.info("❌ Sheet length column not found. Looking for: SheetLength, Sheet_Length, or Length")
    
    if has_sheet_width and has_sheet_length:
        sheet_data = filtered.copy()
        sheet_data[width_col] = pd.to_numeric(sheet_data[width_col], errors="coerce")
        sheet_data[length_col] = pd.to_numeric(sheet_data[length_col], errors="coerce")
        sheet_data = sheet_data.dropna(subset=[width_col, length_col])
        
        # Debug: Show how many sheet records exist
        st.info(f"✓ Found {len(sheet_data)} sheet records in inventory")

        # EXACT SHEET MATCHES: Both width AND length must match exactly
        exact_sheets = sheet_data[
            (sheet_data[width_col].round(2) == round(requested_width, 2)) &
            (sheet_data[length_col].round(2) == round(requested_length, 2))
        ].copy()

        # ALTERNATIVE SHEETS: Width >= requested AND Length >= requested
        alt_sheets = sheet_data[
            (sheet_data[width_col] >= requested_width) &
            (sheet_data[length_col] >= requested_length) &
            ~((sheet_data[width_col].round(2) == round(requested_width, 2)) &
              (sheet_data[length_col].round(2) == round(requested_length, 2)))
        ].copy()

        # Calculate waste for alternative sheets based on area
        if len(alt_sheets) > 0:
            alt_sheets["Requested_Area"] = requested_width * requested_length
            alt_sheets["Actual_Area"] = alt_sheets[width_col] * alt_sheets[length_col]
            alt_sheets["Waste_Area"] = alt_sheets["Actual_Area"] - alt_sheets["Requested_Area"]
            alt_sheets["Waste_Pct"] = (alt_sheets["Waste_Area"] / alt_sheets["Actual_Area"]) * 100.0
            alt_sheets["Splits"] = 1  # Sheets are typically 1:1
            
            # Filter by max waste percentage
            alt_sheets = alt_sheets[alt_sheets["Waste_Pct"] <= max_waste_pct].copy()

    # =========================================================
    # SEARCH FOR ROLLS that can be cut into sheets
    # =========================================================
    roll_results = pd.DataFrame()
    
    if "Roll_Width" in filtered.columns:
        roll_data = filtered.copy()
        roll_data["Roll_Width"] = pd.to_numeric(roll_data["Roll_Width"], errors="coerce")
        roll_data = roll_data.dropna(subset=["Roll_Width"])

        # Rolls must be >= requested width to cut sheets, and <= 65" (Sheeter max)
        SHEETER_MAX_ROLL_WIDTH = 65.0
        suitable_rolls = roll_data[
            (roll_data["Roll_Width"] >= requested_width)
            & (roll_data["Roll_Width"] <= SHEETER_MAX_ROLL_WIDTH)
        ].copy()
        
        if len(suitable_rolls) > 0:
            # Calculate how many sheets can be cut across the width
            suitable_rolls["Splits"] = (suitable_rolls["Roll_Width"] / requested_width).astype(int)
            suitable_rolls["Waste_Inches"] = suitable_rolls["Roll_Width"] - (suitable_rolls["Splits"] * requested_width)
            suitable_rolls["Waste_Pct"] = (suitable_rolls["Waste_Inches"] / suitable_rolls["Roll_Width"]) * 100.0
            
            # Filter by max waste percentage
            roll_results = suitable_rolls[suitable_rolls["Waste_Pct"] <= max_waste_pct].copy()

    # =========================================================
    # PROCESS EXACT MATCHES (sheets only)
    # =========================================================
    if has_sheet_width and has_sheet_length and not exact_sheets.empty:
        ex = exact_sheets.copy()
        if "Caliper" in ex.columns:
            ex["Caliper"] = pd.to_numeric(ex["Caliper"], errors="coerce")
        if width_col in ex.columns:
            ex[width_col] = pd.to_numeric(ex[width_col], errors="coerce")
        if length_col in ex.columns:
            ex[length_col] = pd.to_numeric(ex[length_col], errors="coerce")
        
        # CRITICAL: Convert inventory value to numeric BEFORE groupby
        # (values may have commas like "2,072.29" which makes them strings)
        if inv_col and inv_col in ex.columns:
            ex[inv_col] = ex[inv_col].replace({',': ''}, regex=True)
            ex[inv_col] = pd.to_numeric(ex[inv_col], errors="coerce")

        group_cols_exact = ["GradeName", "BasisWt", "Caliper", width_col, length_col, "Mill", "Brand"]
        group_cols_exact = [c for c in group_cols_exact if c in ex.columns]
        group_cols_exact = _with_reserved(group_cols_exact, ex)

        agg_dict_ex = {"QtyOnHand": "sum"}
        if inv_col and inv_col in ex.columns:
            agg_dict_ex[inv_col] = "sum"
        if "GradeID" in ex.columns:
            agg_dict_ex["GradeID"] = "first"
        if "BasisWtUOM" in ex.columns:
            agg_dict_ex["BasisWtUOM"] = "first"
        _reserve_agg(ex, agg_dict_ex)

        exact_matches = ex.groupby(group_cols_exact, as_index=False, dropna=False).agg(agg_dict_ex)

        # AvgCost = (sum inv) / (sum qty) * 100
        if inv_col and inv_col in exact_matches.columns and "QtyOnHand" in exact_matches.columns:
            exact_matches["AvgCost"] = (
                exact_matches[inv_col] / exact_matches["QtyOnHand"]
            ) * 100.0

        # Drop inventory value column from display
        if inv_col and inv_col in exact_matches.columns:
            exact_matches = exact_matches.drop(columns=[inv_col], errors="ignore")

    # Stamp raw rows with stable IDs so downstream per-roll selection survives reruns
    if not alt_sheets.empty:
        alt_sheets = alt_sheets.reset_index(drop=True)
        alt_sheets["_detail_id"] = alt_sheets.index.astype(int)
    if not roll_results.empty:
        roll_results = roll_results.reset_index(drop=True)
        roll_results["_detail_id"] = roll_results.index.astype(int)

    alternative_sheets = aggregate_alt_sheets(
        alt_sheets, order_quantity, order_size_adj_df, machine_info_df,
        qty_lbs_fn=qty_lbs_fn,
    )
    alternative_rolls = aggregate_alt_rolls(
        roll_results, requested_width, order_quantity, order_size_adj_df,
        grade_df, paper_info_df, machine_info_df, qty_lbs_fn=qty_lbs_fn,
    )

    return exact_matches, alternative_sheets, alternative_rolls, requested_width, alt_sheets, roll_results


# =========================================================
# EXECUTE SEARCH (Unified)
# =========================================================
if search_btn:
    order_quantity_lbs = order_quantity
    order_quantity_sheets = None
    area_in_for_conv = None
    w_for_conv = None
    l_for_conv = None
    gsm_factor_for_conv = None
    sheets_mode_errors = []

    if order_qty_unit == "Sheets":
        # Basis weight is deliberately NOT constrained here: the pounds behind a
        # sheet count are resolved per basis weight during the search, and finally
        # from the lots the user selects. Grade stays single so Area(IN) is fixed.
        if len(grade_names) != 1:
            sheets_mode_errors.append("Order quantity in sheets: select exactly one Grade Name.")
        if not sheet_width_input or not sheet_length_input:
            sheets_mode_errors.append("Order quantity in sheets: Sheet Width and Sheet Length are required.")
        if order_quantity <= 0:
            sheets_mode_errors.append("Order quantity in sheets: enter a positive Order Quantity in sheets.")

        if not sheets_mode_errors:
            try:
                w_for_conv = float(str(sheet_width_input).strip())
                l_for_conv = float(str(sheet_length_input).strip())
            except ValueError:
                sheets_mode_errors.append("Order quantity in sheets: Sheet Width and Sheet Length must be numeric.")
            if grade_df is not None and "Description" in grade_df.columns:
                gm = grade_df[grade_df["Description"].astype(str).str.strip() == str(grade_names[0]).strip()]
                if not gm.empty:
                    area_val = gm.iloc[0].get("Area(IN)")
                    if pd.notna(area_val) and float(area_val) > 0:
                        area_in_for_conv = float(area_val)
                    gsm_val = gm.iloc[0].get("GSM")
                    if pd.notna(gsm_val) and float(gsm_val) > 0:
                        gsm_factor_for_conv = float(gsm_val)
            if area_in_for_conv is None:
                sheets_mode_errors.append(
                    f"Order quantity in sheets: could not find Area(IN) for grade '{grade_names[0]}' in the Grade table."
                )

        if sheets_mode_errors:
            for err in sheets_mode_errors:
                st.error(err)
            st.stop()

        if not gsm_factor_for_conv:
            # Not fatal: only GSM-denominated inventory rows need the factor.
            st.warning(
                f"No GSM factor found for grade '{grade_names[0]}' — any GSM-denominated "
                "lots will not carry an order-size run waste adjustment."
            )

        order_quantity_sheets = int(order_quantity)
        # Pounds stay unknown until lots are selected, since they depend on the
        # basis weight of whatever the user ends up picking.
        order_quantity_lbs = None

    # new search → persist params and clear selections
    st.session_state.search_params = {
        "warehouse_group": warehouse_group,
        "product_group": product_group,
        "grade_names": grade_names,
        "basis_weights": basis_weights,
        "calipers": calipers,
        "sheet_width_input": sheet_width_input,
        "sheet_length_input": sheet_length_input,
        "max_waste_pct": max_waste_pct,
        "order_quantity": order_quantity_lbs,
        "order_qty_unit": order_qty_unit,
        "order_quantity_sheets": order_quantity_sheets,
        "sheets_area_in": area_in_for_conv,
        "sheets_gsm_factor": gsm_factor_for_conv,
        "sheets_width": w_for_conv,
        "sheets_length": l_for_conv,
        "basis_wt_unit": basis_wt_unit,
        "include_reserved": include_reserved,
    }
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_sheets_idx = set()
    st.session_state.sel_alt_rolls_idx = set()
    st.session_state.sel_alt_sheets_detail = set()
    st.session_state.sel_alt_rolls_detail = set()
    st.session_state.seen_alt_sheets_groups = set()
    st.session_state.seen_alt_rolls_groups = set()

exact_matches = pd.DataFrame()
alternative_sheets = pd.DataFrame()
alternative_rolls = pd.DataFrame()
alt_sheets_raw = pd.DataFrame()
alt_rolls_raw = pd.DataFrame()
requested_width = None

if st.session_state.search_params:
    exact_matches, alternative_sheets, alternative_rolls, requested_width, alt_sheets_raw, alt_rolls_raw = run_search(st.session_state.search_params)
else:
    st.info("Use the search form above to run a search.")
    st.stop()


# =========================================================
# DISPLAY: EXACT MATCHES
# =========================================================
st.subheader("🎯 Exact Matches")

# Only surface the reservation column when reserved stock is actually in play.
show_reserved_col = bool(st.session_state.search_params.get("include_reserved"))

if not exact_matches.empty:
    # 10 columns: checkbox, grade, basis, caliper, sheet width, sheet length, mill, brand, qty, cost
    exact_ratios = [0.5, 1.6, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1]
    exact_headers = ["☑", "Grade", "BasisWt", "Caliper", "SheetWidth", "SheetLength",
                     "Mill", "Brand", "QtyOnHand", "AvgCost"]
    if show_reserved_col:
        exact_ratios.append(1.8)
        exact_headers.append("Reserved For")

    header_cols = st.columns(exact_ratios)
    for _i, _title in enumerate(exact_headers):
        with header_cols[_i]:
            st.markdown(f"**{_title}**")
    st.markdown("---")

    for idx, row in exact_matches.iterrows():
        cols = st.columns(exact_ratios)
        key = f"exact_{idx}"

        with cols[0]:
            checked = st.checkbox(
                "sel",
                key=key,
                label_visibility="collapsed",
                value=(idx in st.session_state.sel_exact_idx),
            )
        if checked:
            st.session_state.sel_exact_idx.add(idx)
        else:
            st.session_state.sel_exact_idx.discard(idx)

        with cols[1]:
            st.write(row.get("GradeName", ""))
        with cols[2]:
            v = row.get("BasisWt", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.0f}" if v is not None else "")
        with cols[3]:
            v = row.get("Caliper", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.4f}" if v is not None else "")
        with cols[4]:
            # Try to find sheet width column (dynamically named)
            v = None
            for col in ["SheetWidth", "Sheet_Width", "Width"]:
                if col in row.index:
                    v = row.get(col, None)
                    break
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else "")
        with cols[5]:
            # Try to find sheet length column (dynamically named)
            v = None
            for col in ["SheetLength", "Sheet_Length", "Length"]:
                if col in row.index:
                    v = row.get(col, None)
                    break
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else "")
        with cols[6]:
            st.write(row.get("Mill", ""))
        with cols[7]:
            st.write(row.get("Brand", ""))
        with cols[8]:
            v = row.get("QtyOnHand", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else "")
        with cols[9]:
            v = row.get("AvgCost", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else "")
        if show_reserved_col:
            with cols[10]:
                st.write(_reserved_label(row))

    with st.expander("📋 Exact Details"):
        # Format Caliper to 4 decimal places for display
        display_exact = exact_matches.copy()
        if "Caliper" in display_exact.columns:
            display_exact["Caliper"] = display_exact["Caliper"].apply(lambda x: f"{x:.4f}" if pd.notna(x) else "")
        st.dataframe(display_exact, use_container_width=True)
else:
    params = st.session_state.search_params
    sw = params.get("sheet_width_input")
    sl = params.get("sheet_length_input")
    
    if sw and sl:
        st.info(f"No exact sheet matches for {sw}\" × {sl}\"")
    else:
        st.info("No exact matches found")


# =========================================================
# DISPLAY: ALTERNATIVE SHEETS
# =========================================================
st.subheader("📄 Alternative Sheets")
if not alternative_sheets.empty:
    # Ensure required computed columns exist
    for c in ["Yield", "AvgCost", "NetAvgCost", "ConvertingCostPerCWT", "FinalCostCWT", "RunWastePct"]:
        if c not in alternative_sheets.columns:
            alternative_sheets[c] = None

    # 15 columns for sheets
    sheet_ratios = [
        0.5,  # 0 checkbox
        1.4,  # 1 Grade
        0.8,  # 2 BasisWt
        0.8,  # 3 Caliper
        0.9,  # 4 SheetWidth
        0.9,  # 5 SheetLength
        0.9,  # 6 Mill
        0.9,  # 7 Brand
        1.0,  # 8 Qty
        0.8,  # 9 Waste%
        0.8,  # 10 RunW%
        1.0,  # 11 Yield
        1.0,  # 12 NetAvgCost
        1.0,  # 13 Conv$/CWT
        1.1,  # 14 FinalCost/CWT
    ]

    sheet_headers = [
        "☑", "Grade", "BasisWt", "Caliper", "SheetWidth", "SheetLength",
        "Mill", "Brand", "Qty", "Waste%", "RunW%", "Yield", "NetAvgCost", "Conv$/CWT", "FinalCost/CWT"
    ]
    if show_reserved_col:
        sheet_ratios = sheet_ratios + [1.8]   # 15 Reserved For
        sheet_headers.append("Reserved For")

    H_sh = st.columns(sheet_ratios)
    for i, title in enumerate(sheet_headers):
        with H_sh[i]:
            st.markdown(f"**{title}**")

    st.markdown("---")

    for idx, row in alternative_sheets.iterrows():
        C = st.columns(sheet_ratios)
        key = f"alt_sheet_{idx}"

        with C[0]:
            checked = st.checkbox(
                "sel",
                key=key,
                label_visibility="collapsed",
                value=(idx in st.session_state.sel_alt_sheets_idx),
            )
        if checked:
            st.session_state.sel_alt_sheets_idx.add(idx)
        else:
            st.session_state.sel_alt_sheets_idx.discard(idx)

        with C[1]:  # Grade
            st.write(row.get('GradeName', ''))
        with C[2]:  # BasisWt
            v = row.get('BasisWt')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.0f}" if v is not None else '')
        with C[3]:  # Caliper
            v = row.get('Caliper')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.4f}" if v is not None else '')
        with C[4]:  # SheetWidth
            v = None
            for col in ["SheetWidth", "Sheet_Width"]:
                if col in row.index:
                    v = row.get(col, None)
                    break
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else '')
        with C[5]:  # SheetLength
            v = None
            for col in ["SheetLength", "Sheet_Length"]:
                if col in row.index:
                    v = row.get(col, None)
                    break
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else '')
        with C[6]:  # Mill
            st.write(row.get('Mill', ''))
        with C[7]:  # Brand
            st.write(row.get('Brand', ''))
        with C[8]:  # QtyOnHand
            v = row.get('QtyOnHand')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[9]:  # Waste%
            v = row.get('Waste_Pct')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.1f}%" if v is not None else '')
        with C[10]:  # RunW%
            v = row.get('RunWastePct')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v * 100:.0f}%" if v is not None and v > 0 else '—')
        with C[11]:  # Yield
            v = row.get('Yield')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[12]:  # NetAvgCost
            v = row.get('NetAvgCost')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[13]:  # Conv$/CWT
            v = row.get('ConvertingCostPerCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[14]:  # FinalCost/CWT
            v = row.get('FinalCostCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        if show_reserved_col:
            with C[15]:  # Reserved For
                st.write(_reserved_label(row))

    # Determine sheet width/length column names dynamically (used for group keys)
    sh_group_keys = ["GradeName", "BasisWt", "Caliper", "Mill", "Brand"]
    for col in ["SheetWidth", "Sheet_Width", "Width"]:
        if col in alt_sheets_raw.columns and col in alternative_sheets.columns:
            sh_group_keys.append(col)
            break
    for col in ["SheetLength", "Sheet_Length", "Length"]:
        if col in alt_sheets_raw.columns and col in alternative_sheets.columns:
            sh_group_keys.append(col)
            break
    if "IsReserved" in alt_sheets_raw.columns and "IsReserved" in alternative_sheets.columns:
        sh_group_keys.append("IsReserved")

    # Default-on logic: when an aggregate group is newly selected, auto-select all its rolls
    sync_detail_selection(
        alternative_sheets, alt_sheets_raw,
        st.session_state.sel_alt_sheets_idx,
        sh_group_keys,
        "sel_alt_sheets_detail",
        "seen_alt_sheets_groups",
    )

    with st.expander("📋 Alternative Sheets Details — Pick Individual Rolls", expanded=True):
        sel_sh_idx = sorted(list(st.session_state.sel_alt_sheets_idx))
        if sel_sh_idx and not alt_sheets_raw.empty:
            available_keys = [k for k in sh_group_keys if k in alt_sheets_raw.columns and k in alternative_sheets.columns]
            selected_groups = alternative_sheets.iloc[sel_sh_idx]
            detail_rows = alt_sheets_raw.merge(
                selected_groups[available_keys].drop_duplicates(),
                on=available_keys,
                how="inner",
            )

            if not detail_rows.empty:
                if "InvValue" in detail_rows.columns and "QtyOnHand" in detail_rows.columns:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        detail_rows["CostPerCWT"] = (
                            detail_rows["InvValue"] / detail_rows["QtyOnHand"]
                        ) * 100.0
                        detail_rows["CostPerCWT"] = detail_rows["CostPerCWT"].replace([np.inf, -np.inf], np.nan)

                detail_rows["Selected"] = detail_rows["_detail_id"].astype(int).isin(
                    st.session_state.sel_alt_sheets_detail
                )

                inv_display_cols = [
                    "Selected", "COID", "LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "SheetWidth", "SheetLength", "Condition", "Mill", "Brand",
                    "Warehouse", "QtyOnHand", "Units", "CostPerCWT",
                ]

                # Per-row reservation detail, only when reserved stock is in the pool
                if show_reserved_col and "IsReserved" in detail_rows.columns:
                    detail_rows["Res"] = detail_rows["IsReserved"].map(
                        lambda x: "🔒" if bool(x) else ""
                    )
                    inv_display_cols.insert(1, "Res")
                    inv_display_cols += ["ResSONum", "ReserveSalesRep", "ResCust"]

                available_display = [c for c in inv_display_cols if c in detail_rows.columns]
                # Always include _detail_id so we can map edits back, even though it's hidden
                editor_df = detail_rows[available_display + ["_detail_id"]].copy()

                col_config = {
                    "_detail_id": None,
                    "Selected": st.column_config.CheckboxColumn("✓", default=True),
                }
                if "QtyOnHand" in editor_df.columns:
                    col_config["QtyOnHand"] = st.column_config.NumberColumn("QtyOnHand", format="%.0f")
                if "BasisWt" in editor_df.columns:
                    col_config["BasisWt"] = st.column_config.NumberColumn("BasisWt", format="%d")
                if "Caliper" in editor_df.columns:
                    col_config["Caliper"] = st.column_config.NumberColumn("Caliper", format="%.4f")
                if "CostPerCWT" in editor_df.columns:
                    col_config["CostPerCWT"] = st.column_config.NumberColumn("Cost/CWT", format="$%.2f")

                # Key changes when the visible-row set changes, so stale edits
                # from a different aggregate selection can't bleed onto new rows.
                _vid_sh = tuple(sorted(editor_df["_detail_id"].astype(int).tolist()))
                _editor_key_sh = f"alt_sheets_detail_editor_{hash(_vid_sh)}"

                _btn_col_sh, _ = st.columns([1, 5])
                with _btn_col_sh:
                    if st.button("☐ Unselect All", key=f"unsel_all_sheets_{hash(_vid_sh)}"):
                        st.session_state.sel_alt_sheets_detail -= set(_vid_sh)
                        if _editor_key_sh in st.session_state:
                            del st.session_state[_editor_key_sh]
                        st.rerun()

                # Refresh the Selected column from session state (Unselect All may have just modified it)
                editor_df["Selected"] = editor_df["_detail_id"].astype(int).isin(
                    st.session_state.sel_alt_sheets_detail
                )

                edited = st.data_editor(
                    editor_df,
                    column_config=col_config,
                    disabled=[c for c in editor_df.columns if c != "Selected"],
                    hide_index=True,
                    use_container_width=True,
                    key=_editor_key_sh,
                )

                visible_ids = set(_vid_sh)
                selected_now = set(edited.loc[edited["Selected"] == True, "_detail_id"].astype(int).tolist())
                st.session_state.sel_alt_sheets_detail = (
                    (st.session_state.sel_alt_sheets_detail - visible_ids) | selected_now
                )
            else:
                st.info("No underlying inventory rows found for the selected alternatives.")
        else:
            st.info("Select alternative sheet rows above to see underlying inventory detail.")
else:
    params = st.session_state.search_params
    mw = params.get("max_waste_pct", 10.0)
    sw = params.get("sheet_width_input")
    sl = params.get("sheet_length_input")
    
    if sw and sl:
        st.info(f"No alternative sheets within {mw}% waste for {sw}\" × {sl}\"")
    else:
        st.info("No alternative sheets found")


# =========================================================
# DISPLAY: ALTERNATIVE ROLLS
# =========================================================
st.subheader("🎞️ Alternative Rolls")
st.markdown("**Maximum roll width options presented = sheeter max width of 65 inch**")
if not alternative_rolls.empty:
    # Ensure required computed columns exist
    for c in ["Yield", "AvgCost", "NetAvgCost", "LbsPerHour", "ConvHrs", "ConvertingCostPerCWT", "FinalCostCWT", "RunWastePct"]:
        if c not in alternative_rolls.columns:
            alternative_rolls[c] = None

    # 17 columns for rolls
    roll_ratios = [
        0.5,  # 0 checkbox
        1.4,  # 1 Grade
        0.8,  # 2 BasisWt
        0.8,  # 3 Caliper
        0.9,  # 4 RollWidth
        0.9,  # 5 Mill
        0.9,  # 6 Brand
        1.0,  # 7 Qty
        0.7,  # 8 Splits
        0.8,  # 9 Waste%
        0.8,  # 10 RunW%
        1.0,  # 11 Yield
        1.0,  # 12 NetAvgCost
        1.0,  # 13 Lbs/Hr
        0.9,  # 14 ConvHrs
        1.0,  # 15 Conv$/CWT
        1.1   # 16 FinalCost/CWT
    ]

    roll_headers = [
        "☑", "Grade", "BasisWt", "Caliper", "RollWidth",
        "Mill", "Brand", "Qty", "Splits", "Waste%", "RunW%", "Yield", "NetAvgCost",
        "Lbs/Hr", "ConvHrs", "Conv$/CWT", "FinalCost/CWT"
    ]
    if show_reserved_col:
        roll_ratios = roll_ratios + [1.8]   # 17 Reserved For
        roll_headers.append("Reserved For")

    H_rl = st.columns(roll_ratios)
    for i, title in enumerate(roll_headers):
        with H_rl[i]:
            st.markdown(f"**{title}**")

    st.markdown("---")

    for idx, row in alternative_rolls.iterrows():
        C = st.columns(roll_ratios)
        key = f"alt_roll_{idx}"

        with C[0]:
            checked = st.checkbox(
                "sel",
                key=key,
                label_visibility="collapsed",
                value=(idx in st.session_state.sel_alt_rolls_idx),
            )
        if checked:
            st.session_state.sel_alt_rolls_idx.add(idx)
        else:
            st.session_state.sel_alt_rolls_idx.discard(idx)

        with C[1]:  # Grade
            st.write(row.get('GradeName', ''))
        with C[2]:  # BasisWt
            v = row.get('BasisWt')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.0f}" if v is not None else '')
        with C[3]:  # Caliper
            v = row.get('Caliper')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.4f}" if v is not None else '')
        with C[4]:  # RollWidth
            v = row.get('Roll_Width')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else '')
        with C[5]:  # Mill
            st.write(row.get('Mill', ''))
        with C[6]:  # Brand
            st.write(row.get('Brand', ''))
        with C[7]:  # QtyOnHand
            v = row.get('QtyOnHand')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[8]:  # Splits
            v = row.get('Splits')
            st.write(f"{int(v)}x" if pd.notna(v) else '')
        with C[9]:  # Waste%
            v = row.get('Waste_Pct')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.1f}%" if v is not None else '')
        with C[10]:  # RunW%
            v = row.get('RunWastePct')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v * 100:.0f}%" if v is not None and v > 0 else '—')
        with C[11]:  # Yield
            v = row.get('Yield')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[12]:  # NetAvgCost
            v = row.get('NetAvgCost')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[13]:  # Lbs/Hr
            v = row.get('LbsPerHour')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[14]:  # ConvHrs
            v = row.get('ConvHrs')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.1f}h" if v is not None else '')
        with C[15]:  # Conv$/CWT
            v = row.get('ConvertingCostPerCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[16]:  # FinalCost/CWT
            v = row.get('FinalCostCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        if show_reserved_col:
            with C[17]:  # Reserved For
                st.write(_reserved_label(row))

    rl_group_keys = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "Mill", "Brand"]
    if "IsReserved" in alt_rolls_raw.columns and "IsReserved" in alternative_rolls.columns:
        rl_group_keys.append("IsReserved")

    sync_detail_selection(
        alternative_rolls, alt_rolls_raw,
        st.session_state.sel_alt_rolls_idx,
        rl_group_keys,
        "sel_alt_rolls_detail",
        "seen_alt_rolls_groups",
    )

    with st.expander("📋 Alternative Rolls Details — Pick Individual Rolls", expanded=True):
        sel_rl_idx = sorted(list(st.session_state.sel_alt_rolls_idx))
        if sel_rl_idx and not alt_rolls_raw.empty:
            available_keys = [k for k in rl_group_keys if k in alt_rolls_raw.columns and k in alternative_rolls.columns]
            selected_groups = alternative_rolls.iloc[sel_rl_idx]
            detail_rows = alt_rolls_raw.merge(
                selected_groups[available_keys].drop_duplicates(),
                on=available_keys,
                how="inner",
            )

            if not detail_rows.empty:
                if "InvValue" in detail_rows.columns and "QtyOnHand" in detail_rows.columns:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        detail_rows["CostPerCWT"] = (
                            detail_rows["InvValue"] / detail_rows["QtyOnHand"]
                        ) * 100.0
                        detail_rows["CostPerCWT"] = detail_rows["CostPerCWT"].replace([np.inf, -np.inf], np.nan)

                detail_rows["Selected"] = detail_rows["_detail_id"].astype(int).isin(
                    st.session_state.sel_alt_rolls_detail
                )

                inv_display_cols = [
                    "Selected", "COID", "LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "Roll_Width", "Diameter", "Condition", "Mill", "Brand",
                    "Warehouse", "QtyOnHand", "Units", "CostPerCWT",
                ]

                # Per-roll reservation detail, only when reserved stock is in the pool
                if show_reserved_col and "IsReserved" in detail_rows.columns:
                    detail_rows["Res"] = detail_rows["IsReserved"].map(
                        lambda x: "🔒" if bool(x) else ""
                    )
                    inv_display_cols.insert(1, "Res")
                    inv_display_cols += ["ResSONum", "ReserveSalesRep", "ResCust"]

                available_display = [c for c in inv_display_cols if c in detail_rows.columns]
                editor_df = detail_rows[available_display + ["_detail_id"]].copy()

                col_config = {
                    "_detail_id": None,
                    "Selected": st.column_config.CheckboxColumn("✓", default=True),
                }
                if "QtyOnHand" in editor_df.columns:
                    col_config["QtyOnHand"] = st.column_config.NumberColumn("QtyOnHand", format="%.0f")
                if "BasisWt" in editor_df.columns:
                    col_config["BasisWt"] = st.column_config.NumberColumn("BasisWt", format="%d")
                if "Caliper" in editor_df.columns:
                    col_config["Caliper"] = st.column_config.NumberColumn("Caliper", format="%.4f")
                if "Roll_Width" in editor_df.columns:
                    col_config["Roll_Width"] = st.column_config.NumberColumn("Roll_Width", format="%.2f")
                if "Diameter" in editor_df.columns:
                    col_config["Diameter"] = st.column_config.NumberColumn("Diameter", format="%.0f")
                if "CostPerCWT" in editor_df.columns:
                    col_config["CostPerCWT"] = st.column_config.NumberColumn("Cost/CWT", format="$%.2f")

                _vid_rl = tuple(sorted(editor_df["_detail_id"].astype(int).tolist()))
                _editor_key_rl = f"alt_rolls_detail_editor_{hash(_vid_rl)}"

                _btn_col_rl, _ = st.columns([1, 5])
                with _btn_col_rl:
                    if st.button("☐ Unselect All", key=f"unsel_all_rolls_{hash(_vid_rl)}"):
                        st.session_state.sel_alt_rolls_detail -= set(_vid_rl)
                        if _editor_key_rl in st.session_state:
                            del st.session_state[_editor_key_rl]
                        st.rerun()

                # Refresh the Selected column from session state (Unselect All may have just modified it)
                editor_df["Selected"] = editor_df["_detail_id"].astype(int).isin(
                    st.session_state.sel_alt_rolls_detail
                )

                edited = st.data_editor(
                    editor_df,
                    column_config=col_config,
                    disabled=[c for c in editor_df.columns if c != "Selected"],
                    hide_index=True,
                    use_container_width=True,
                    key=_editor_key_rl,
                )

                visible_ids = set(_vid_rl)
                selected_now = set(edited.loc[edited["Selected"] == True, "_detail_id"].astype(int).tolist())
                st.session_state.sel_alt_rolls_detail = (
                    (st.session_state.sel_alt_rolls_detail - visible_ids) | selected_now
                )
            else:
                st.info("No underlying inventory rows found for the selected alternatives.")
        else:
            st.info("Select alternative roll rows above to see underlying inventory detail.")
else:
    params = st.session_state.search_params
    mw = params.get("max_waste_pct", 10.0)
    sw = params.get("sheet_width_input")
    
    if sw:
        st.info(f"No alternative rolls within {mw}% waste for width {sw}\"")
    else:
        st.info("No alternative rolls found")


# =========================================================
# SUMMARY OF SELECTED + CSV EXPORT
# =========================================================
st.markdown("---")
st.subheader("📊 Summary of Selected")

exact_sel_idx_sorted = sorted(list(st.session_state.sel_exact_idx))
alt_sheets_sel_idx_sorted = sorted(list(st.session_state.sel_alt_sheets_idx))
alt_rolls_sel_idx_sorted = sorted(list(st.session_state.sel_alt_rolls_idx))

selected_exact = (
    exact_matches.iloc[exact_sel_idx_sorted]
    if (not exact_matches.empty and exact_sel_idx_sorted)
    else pd.DataFrame()
)

# Re-aggregate alt sheets / rolls from the user's per-individual-roll detail selection,
# so the summary reflects exactly the rolls checked in the detail expanders.
_sp = st.session_state.search_params
_order_qty = _sp.get("order_quantity")
# Sheets mode: same per-basis-weight resolver the search used, so the re-aggregated
# selection carries the same run waste as the rows the user was looking at.
_qty_lbs_fn = make_qty_lbs_fn(_sp)
_req_width = None
try:
    _req_width = float(str(_sp.get("sheet_width_input")).strip()) if _sp.get("sheet_width_input") else None
except (TypeError, ValueError):
    _req_width = None

if (
    not alt_sheets_raw.empty
    and st.session_state.sel_alt_sheets_detail
):
    _picked_sh = alt_sheets_raw[
        alt_sheets_raw["_detail_id"].astype(int).isin(st.session_state.sel_alt_sheets_detail)
    ]
    selected_alt_sheets = aggregate_alt_sheets(
        _picked_sh, _order_qty, order_size_adj_df, machine_info_df,
        qty_lbs_fn=_qty_lbs_fn,
    )
else:
    selected_alt_sheets = pd.DataFrame()

if (
    not alt_rolls_raw.empty
    and st.session_state.sel_alt_rolls_detail
):
    _picked_rl = alt_rolls_raw[
        alt_rolls_raw["_detail_id"].astype(int).isin(st.session_state.sel_alt_rolls_detail)
    ]
    selected_alt_rolls = aggregate_alt_rolls(
        _picked_rl, _req_width, _order_qty, order_size_adj_df,
        grade_df, paper_info_df, machine_info_df, qty_lbs_fn=_qty_lbs_fn,
    )
else:
    selected_alt_rolls = pd.DataFrame()

# Totals
if not selected_exact.empty and "QtyOnHand" in selected_exact.columns:
    total_exact_lbs = selected_exact["QtyOnHand"].fillna(0.0).sum()
else:
    total_exact_lbs = 0.0

total_alt_sheets_yield = 0.0
if not selected_alt_sheets.empty and "Yield" in selected_alt_sheets.columns:
    total_alt_sheets_yield = selected_alt_sheets["Yield"].fillna(0.0).sum()

total_alt_rolls_yield = 0.0
if not selected_alt_rolls.empty and "Yield" in selected_alt_rolls.columns:
    total_alt_rolls_yield = selected_alt_rolls["Yield"].fillna(0.0).sum()

total_alt_yield = total_alt_sheets_yield + total_alt_rolls_yield
total_lbs = total_exact_lbs + total_alt_yield

# --- Reserved material in the selection -----------------------------------
# Exact matches are quoted at QtyOnHand; alternatives (sheets and rolls) at Yield.
_reserved_lbs = 0.0
_reserved_holders = []
for _frame, _lbs_col in (
    (selected_exact, "QtyOnHand"),
    (selected_alt_sheets, "Yield"),
    (selected_alt_rolls, "Yield"),
):
    if _frame is None or _frame.empty or "IsReserved" not in _frame.columns:
        continue
    _res = _frame[_frame["IsReserved"].fillna(False).astype(bool)]
    if _res.empty:
        continue
    _col = _lbs_col if _lbs_col in _res.columns else "QtyOnHand"
    _reserved_lbs += float(pd.to_numeric(_res.get(_col), errors="coerce").fillna(0.0).sum())
    for _, _r in _res.iterrows():
        _cust = _clean_val(_r.get("ResCust", ""))
        _so = _clean_val(_r.get("ResSONum", ""))
        _rep = _clean_val(_r.get("ReserveSalesRep", ""))
        _who = f"{_cust} (SO# {_so})" if (_cust and _so) else (_cust or (f"SO# {_so}" if _so else ""))
        if _rep:
            _who = f"{_who} — {_rep}" if _who else _rep
        if _who:
            _reserved_holders.append(_who)
_reserved_holders = sorted(set(_reserved_holders))

if _reserved_lbs > 0:
    _pct = (_reserved_lbs / total_lbs * 100.0) if total_lbs else 0.0
    st.warning(
        f"🔒 **Scenario includes reserved material** — {_reserved_lbs:,.0f} lbs "
        f"({_pct:.0f}% of this quote) is currently reserved. "
        "Confirm release with the owning sales rep before committing."
    )
    if _reserved_holders:
        with st.expander(f"🔒 Reserved for ({len(_reserved_holders)})"):
            for _h in _reserved_holders:
                st.markdown(f"- {_h}")

# Dollar values
if not selected_exact.empty and set(["AvgCost", "QtyOnHand"]).issubset(selected_exact.columns):
    exact_value = (
        (selected_exact["AvgCost"].fillna(0.0) / 100.0)
        * selected_exact["QtyOnHand"].fillna(0.0)
    ).sum()
else:
    exact_value = 0.0

alt_sheets_value = 0.0
if not selected_alt_sheets.empty and set(["FinalCostCWT", "Yield"]).issubset(selected_alt_sheets.columns):
    alt_sheets_value = (
        (selected_alt_sheets["FinalCostCWT"].fillna(0.0) / 100.0)
        * selected_alt_sheets["Yield"].fillna(0.0)
    ).sum()

alt_rolls_value = 0.0
if not selected_alt_rolls.empty and set(["FinalCostCWT", "Yield"]).issubset(selected_alt_rolls.columns):
    alt_rolls_value = (
        (selected_alt_rolls["FinalCostCWT"].fillna(0.0) / 100.0)
        * selected_alt_rolls["Yield"].fillna(0.0)
    ).sum()

total_value = exact_value + alt_sheets_value + alt_rolls_value

if total_lbs > 0:
    blended_cost_cwt = (total_value / total_lbs) * 100.0
else:
    blended_cost_cwt = 0.0

# Calculate Mweight (lbs per 1000 sheets)
# Formula: ((Width * Length) / Area(IN)) * Basis Weight in Lbs * 2
mweight = None
mweight_error = None
params = st.session_state.search_params
sheet_width = params.get("sheet_width_input")
sheet_length = params.get("sheet_length_input")

if sheet_width and sheet_length:
    # Combine all selected rows to get BasisWt and GradeID
    combined_selected = pd.concat([selected_exact, selected_alt_sheets, selected_alt_rolls], ignore_index=True) if (
        not selected_exact.empty or not selected_alt_sheets.empty or not selected_alt_rolls.empty
    ) else pd.DataFrame()
    
    if not combined_selected.empty:
        # Check if all selected rows have the same BasisWt
        if "BasisWt" in combined_selected.columns:
            unique_basis_wts = combined_selected["BasisWt"].dropna().unique()
            if len(unique_basis_wts) > 1:
                mweight_error = "Mweight cannot be calculated when non-uniform basis weights are selected"
            elif len(unique_basis_wts) == 1:
                # Get BasisWt and BasisWtUOM from the first selected row
                selected_basis_wt = float(unique_basis_wts[0])
                basis_uom = "LB"
                grade_name = combined_selected.iloc[0].get("GradeName", "Unknown")
                
                if "BasisWtUOM" in combined_selected.columns:
                    uom_val = combined_selected.iloc[0].get("BasisWtUOM")
                    if pd.notna(uom_val):
                        basis_uom = str(uom_val).strip().upper()
                
                # Look up Area(IN) and GSM from Grade table based on GradeID
                area_in = None
                gsm_factor = None
                if grade_df is not None and "GradeID" in combined_selected.columns:
                    grade_id = str(combined_selected.iloc[0].get("GradeID", "")).strip()
                    if grade_id:
                        grade_match = grade_df[grade_df["GradeID"] == grade_id]
                        if not grade_match.empty:
                            area_in = float(grade_match.iloc[0].get("Area(IN)", 0) or 0)
                            gsm_val = grade_match.iloc[0].get("GSM")
                            if pd.notna(gsm_val) and float(gsm_val) > 0:
                                gsm_factor = float(gsm_val)
                
                # Convert basis weight to LBS if needed
                if selected_basis_wt and selected_basis_wt > 0:
                    if basis_uom == "GSM":
                        if gsm_factor and gsm_factor > 0:
                            basis_wt_lbs = selected_basis_wt / gsm_factor
                        else:
                            mweight_error = f"No GSM_Factor found for grade {grade_name}"
                            basis_wt_lbs = None
                    else:
                        basis_wt_lbs = selected_basis_wt
                    
                    # Calculate Mweight: ((Width * Length) / Area(IN)) * BasisWt in LBS * 2
                    if basis_wt_lbs and area_in and area_in > 0:
                        mweight = round(((float(sheet_width) * float(sheet_length)) / area_in) * basis_wt_lbs * 2)

# Summary metrics row (always visible – Option A)
# Calculate Cost Per M Sheets: Cost Per CWT * .01 * Mweight (using rounded Mweight)
cost_per_m = None
if mweight and mweight > 0 and blended_cost_cwt > 0:
    cost_per_m = blended_cost_cwt * 0.01 * mweight

# Calculate Estimated Sheets: (Total Weight / Mweight) * 1000 (using rounded Mweight)
est_sheets = None
if mweight and mweight > 0 and total_lbs > 0:
    est_sheets = (total_lbs / mweight) * 1000

# --- Order quantity in lbs ---
# LBS mode has it from the search form. Sheets mode defers it to here: the pounds
# behind the requested sheet count follow from the Mweight of the selected lots,
# so it stays None until a selection resolves a single Mweight. Everything
# downstream (surcharge bracket, machine minimum, freight) is unchanged.
order_quantity_param = st.session_state.search_params.get("order_quantity")
_sheets_requested = st.session_state.search_params.get("order_quantity_sheets")
if st.session_state.search_params.get("order_qty_unit") == "Sheets":
    order_quantity_param = (
        (_sheets_requested * mweight) / 1000.0
        if (_sheets_requested and mweight and mweight > 0)
        else None
    )

# --- Blended converting cost (raw) and order-adjusted converting cost ---
blended_conv_cwt = 0.0
final_conv_cwt = None
order_qty_cost_cwt = None
order_qty_cost_per_m = None
conv_breakdown = None

# Yield-weighted converting cost across BOTH alternative tables (sheets + rolls)
alt_conv_dollar = 0.0
alt_yield_for_conv = 0.0

if (
    not selected_alt_sheets.empty
    and "ConvertingCostPerCWT" in selected_alt_sheets.columns
    and "Yield" in selected_alt_sheets.columns
):
    alt_conv_dollar += (
        (selected_alt_sheets["ConvertingCostPerCWT"].fillna(0.0) / 100.0)
        * selected_alt_sheets["Yield"].fillna(0.0)
    ).sum()
    alt_yield_for_conv += selected_alt_sheets["Yield"].fillna(0.0).sum()

if (
    not selected_alt_rolls.empty
    and "ConvertingCostPerCWT" in selected_alt_rolls.columns
    and "Yield" in selected_alt_rolls.columns
):
    alt_conv_dollar += (
        (selected_alt_rolls["ConvertingCostPerCWT"].fillna(0.0) / 100.0)
        * selected_alt_rolls["Yield"].fillna(0.0)
    ).sum()
    alt_yield_for_conv += selected_alt_rolls["Yield"].fillna(0.0).sum()

if alt_yield_for_conv > 0:
    blended_conv_cwt = (alt_conv_dollar / alt_yield_for_conv) * 100.0

if (
    (not selected_alt_sheets.empty or not selected_alt_rolls.empty)
    and order_quantity_param
    and order_quantity_param > 0
):
    equip_type_sel = "Sheeter"

    # --- Alt sheets: flat Trimmer rate × yield (per-row model, unchanged) ---
    alt_sheets_job_dollar = 0.0
    if (
        not selected_alt_sheets.empty
        and "ConvertingCostPerCWT" in selected_alt_sheets.columns
        and "Yield" in selected_alt_sheets.columns
    ):
        alt_sheets_job_dollar = (
            (selected_alt_sheets["ConvertingCostPerCWT"].fillna(0.0) / 100.0)
            * selected_alt_sheets["Yield"].fillna(0.0)
        ).sum()

    # --- Alt rolls: NCC-style JOB-level calc (setup once, roll changes totalled,
    # processing scaled by rate_qty = max(order_qty, 20000) for flat base rate) ---
    alt_rolls_job_dollar = 0.0
    if (
        not selected_alt_rolls.empty
        and "LbsPerHour" in selected_alt_rolls.columns
        and "Yield" in selected_alt_rolls.columns
        and machine_info_df is not None
    ):
        y_rolls = selected_alt_rolls["Yield"].fillna(0.0)
        lph_rolls = selected_alt_rolls["LbsPerHour"].replace(0, np.nan)
        total_y_rolls = y_rolls.sum()
        total_proc_hrs_yield = (y_rolls / lph_rolls).sum()

        if total_y_rolls > 0 and total_proc_hrs_yield > 0:
            effective_lbs_per_hour = total_y_rolls / total_proc_hrs_yield

            total_units = (
                selected_alt_rolls["Units"].fillna(0.0).sum()
                if "Units" in selected_alt_rolls.columns else 0.0
            )
            avg_yield_per_roll = (total_y_rolls / total_units) if total_units > 0 else total_y_rolls

            # Rolls running simultaneously (Sheeter + thin stock → NumShtrRolls)
            first_caliper = float(selected_alt_rolls.iloc[0].get("Caliper", 0) or 0)
            rolls_running = 1
            if first_caliper > 0 and first_caliper <= 0.011 and paper_info_df is not None:
                first_pg = str(selected_alt_rolls.iloc[0].get("ProductGroupID", "")).strip()
                if first_pg and "ProductGroupID" in paper_info_df.columns:
                    pr = paper_info_df[
                        paper_info_df["ProductGroupID"].astype(str).str.strip() == first_pg
                    ]
                    if not pr.empty:
                        nsr = pr.iloc[0].get("NumShtrRolls")
                        if nsr is not None and pd.notna(nsr) and float(nsr) > 0:
                            rolls_running = int(float(nsr))

            mr_sheeter = machine_info_df[
                machine_info_df["EquipType"].astype(str).str.strip() == equip_type_sel
            ]
            if not mr_sheeter.empty:
                sheeter_hr = float(mr_sheeter.iloc[0].get("HourlyRate", 273.0) or 273.0)
                sheeter_setup = float(mr_sheeter.iloc[0].get("Setup_Hrs", 0.3) or 0.3)
                sheeter_rc = float(mr_sheeter.iloc[0].get("Roll_Change_Hrs", 0.0) or 0.0)

                # Base rate held flat at 20k+ lbs (NCC convention)
                rate_qty = max(order_quantity_param, 20000)

                rolls_needed = int(np.ceil(rate_qty / avg_yield_per_roll)) if avg_yield_per_roll > 0 else 1
                roll_change_hrs_job = (
                    int(np.ceil(rolls_needed / rolls_running)) * sheeter_rc
                ) if rolls_running > 0 else 0
                processing_hrs = rate_qty / effective_lbs_per_hour
                total_hrs = processing_hrs + roll_change_hrs_job + sheeter_setup
                cost_at_rate = total_hrs * sheeter_hr

                # Pro-rate to customer's actual order quantity
                alt_rolls_job_dollar = cost_at_rate * (order_quantity_param / rate_qty)

    raw_conv_dollar = alt_sheets_job_dollar + alt_rolls_job_dollar

    # Machine minimum charge (Sheeter)
    min_chg = 0.0
    if machine_info_df is not None and "EquipType" in machine_info_df.columns:
        mr = machine_info_df[
            machine_info_df["EquipType"].astype(str).str.strip() == equip_type_sel
        ]
        if not mr.empty:
            min_chg = float(mr.iloc[0].get("Minimum Charge", 0.0) or 0.0)

    # OrderQty bracket surcharge
    surcharge_pct = 0.0
    if order_size_adj_df is not None:
        surcharge_pct = get_order_size_pct(
            order_size_adj_df, equip_type_sel, "OrderQty", order_quantity_param
        )

    # Apply OrderQty surcharge first, then compare against machine minimum
    surcharged_dollar = raw_conv_dollar * (1 + surcharge_pct)
    final_conv_dollar = max(surcharged_dollar, min_chg) if min_chg > 0 else surcharged_dollar
    final_conv_cwt = (final_conv_dollar / order_quantity_param) * 100.0

    conv_breakdown = {
        "base_cwt": blended_conv_cwt,
        "surcharge_pct": surcharge_pct,
        "surcharged_cwt": (surcharged_dollar / order_quantity_param) * 100.0,
        "min_chg": min_chg,
        "min_applies": min_chg > 0 and surcharged_dollar < min_chg,
        "final_conv_cwt": final_conv_cwt,
    }

    # Order Qty Cost = paper portion of blended + order-adjusted converting
    order_qty_cost_cwt = (blended_cost_cwt - blended_conv_cwt) + final_conv_cwt
    if mweight and mweight > 0:
        order_qty_cost_per_m = order_qty_cost_cwt * 0.01 * mweight

# --- Freight: convert total $ to $/CWT and fold into Order Qty Cost ---
freight_cwt = 0.0
freight_per_m = 0.0
if freight_cost and freight_cost > 0 and order_quantity_param and order_quantity_param > 0:
    freight_cwt = (freight_cost / order_quantity_param) * 100.0
    if mweight and mweight > 0:
        freight_per_m = freight_cwt * 0.01 * mweight
    if order_qty_cost_cwt is not None:
        order_qty_cost_cwt += freight_cwt
    if order_qty_cost_per_m is not None:
        order_qty_cost_per_m += freight_per_m

# Shrink metric label/value fonts so 10 columns fit without wrapping
st.markdown(
    """
    <style>
    div[data-testid="stMetric"] > label { font-size: 0.75rem; }
    div[data-testid="stMetricValue"] > div { font-size: 1.05rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

c1, c2, c3, c4, c5, c6, c7, c8, c9, c10 = st.columns(10)
with c1:
    st.metric("Exact Qty Selected", f"{total_exact_lbs:,.0f} lbs")
with c2:
    st.metric("Alt Yield Selected", f"{total_alt_yield:,.0f} lbs")
with c3:
    st.metric("Total Estimated Yield", f"{total_lbs:,.0f} lbs")
with c4:
    st.metric("Mweight", f"{mweight:,.0f} lbs" if mweight is not None else "—")
with c5:
    st.metric("Blended Cost", f"${blended_cost_cwt:,.2f} / CWT")
with c6:
    st.metric("Cost Per M Sheets", f"${cost_per_m:,.2f}" if cost_per_m is not None else "—")
with c7:
    st.metric("Est. Sheets", f"{est_sheets:,.0f}" if est_sheets is not None else "—")
with c8:
    sheets_param = st.session_state.search_params.get("order_quantity_sheets")
    _sheets_mode = st.session_state.search_params.get("order_qty_unit") == "Sheets"
    if _sheets_mode and sheets_param:
        # Sheets are known from the search; pounds only once a selection fixes Mweight.
        oq_value = f"{sheets_param:,} sht"
        oq_help = (
            f"≈ {order_quantity_param:,.0f} lbs (converted from {sheets_param:,} sheets)"
            if order_quantity_param
            else "Weight pending — select lots with a single basis weight to resolve Mweight."
        )
        st.metric("Order Qty", oq_value, help=oq_help)
    elif order_quantity_param:
        st.metric("Order Qty", f"{order_quantity_param:,.0f} lbs")
    else:
        st.metric("Order Qty", "—")
with c9:
    cwt_help = f"Includes ${freight_cwt:,.2f}/CWT freight (${freight_cost:,.2f} total)" if freight_cwt > 0 else None
    st.metric(
        "Order Qty Cost",
        f"${order_qty_cost_cwt:,.2f} / CWT" if order_qty_cost_cwt is not None else "— / CWT",
        help=cwt_help,
    )
with c10:
    per_m_help = f"Includes ${freight_per_m:,.2f}/M sheets freight (${freight_cost:,.2f} total)" if freight_per_m > 0 else None
    st.metric(
        "Order Qty Cost",
        f"${order_qty_cost_per_m:,.2f} / M Sheets" if order_qty_cost_per_m is not None else "— / M Sheets",
        help=per_m_help,
    )

if mweight_error:
    st.error(mweight_error)

# --- Order Qty Cost breakdown (base rate, upcharge, minimum, freight) ---
if conv_breakdown is not None and order_qty_cost_cwt is not None:
    with st.expander("Order Qty Cost breakdown"):
        paper_cwt = blended_cost_cwt - conv_breakdown["base_cwt"]
        rows = [("Base converting rate", f"${conv_breakdown['base_cwt']:,.2f} / CWT")]
        if conv_breakdown["surcharge_pct"] > 0:
            rows.append((
                "Order-qty upcharge",
                f"+{conv_breakdown['surcharge_pct'] * 100:.0f}%  →  ${conv_breakdown['surcharged_cwt']:,.2f} / CWT",
            ))
        else:
            rows.append(("Order-qty upcharge", "none"))
        if conv_breakdown["min_chg"] > 0:
            rows.append((
                "Minimum charge",
                f"${conv_breakdown['min_chg']:,.2f}"
                + ("  (applied)" if conv_breakdown["min_applies"] else "  (not reached)"),
            ))
        else:
            rows.append(("Minimum charge", "none"))
        rows.append(("Converting cost for order", f"${conv_breakdown['final_conv_cwt']:,.2f} / CWT"))
        rows.append(("Paper cost (blended)", f"${paper_cwt:,.2f} / CWT"))
        if freight_cwt > 0:
            rows.append((
                "Freight",
                f"${freight_cwt:,.2f} / CWT  (${freight_cost:,.2f} total)",
            ))
        else:
            rows.append(("Freight", "none"))
        rows.append((
            "Order Qty Cost (total)",
            f"${order_qty_cost_cwt:,.2f} / CWT"
            + (f"  ·  ${order_qty_cost_per_m:,.2f} / M Sheets" if order_qty_cost_per_m is not None else ""),
        ))
        st.table(pd.DataFrame(rows, columns=["Item", "Amount"]).set_index("Item"))

if selected_exact.empty and selected_alt_sheets.empty and selected_alt_rolls.empty:
    st.info("No rows selected yet above.")

# CSV Export of selected rows
export_df = pd.concat([selected_exact, selected_alt_sheets, selected_alt_rolls], ignore_index=True) if (
    not selected_exact.empty or not selected_alt_sheets.empty or not selected_alt_rolls.empty
) else pd.DataFrame()

# Build per-roll detail (the individual rolls the user kept checked in the detail expanders)
def _build_detail_rows(raw, detail_ids):
    if raw is None or raw.empty or not detail_ids:
        return pd.DataFrame()
    out = raw[raw["_detail_id"].astype(int).isin(detail_ids)].copy()
    if "InvValue" in out.columns and "QtyOnHand" in out.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            out["CostPerCWT"] = (out["InvValue"] / out["QtyOnHand"]) * 100.0
            out["CostPerCWT"] = out["CostPerCWT"].replace([np.inf, -np.inf], np.nan)
    return out

detail_sheets_df = _build_detail_rows(alt_sheets_raw, st.session_state.sel_alt_sheets_detail)
detail_rolls_df = _build_detail_rows(alt_rolls_raw, st.session_state.sel_alt_rolls_detail)

if not export_df.empty:
    # Build basis weight token for filename (use first selected or empty if none/multiple)
    params = st.session_state.search_params
    basis_weights_selected = params.get("basis_weights", [])
    if len(basis_weights_selected) == 1:
        bw_token = str(basis_weights_selected[0])
    elif len(basis_weights_selected) > 1:
        bw_token = "multi"
    else:
        bw_token = ""
    
    # Build dimension token for sheets
    dim_token = ""
    if sheet_width_input and sheet_length_input:
        dim_token = f"{sheet_width_input}x{sheet_length_input}"
    
    fname = "NKQuote.csv"
    pdf_fname = "NKQuote.pdf"
    if bw_token and dim_token:
        fname = f"NKQuote_{bw_token}_{dim_token}.csv"
        pdf_fname = f"NKQuote_{bw_token}_{dim_token}.pdf"
    elif bw_token:
        fname = f"NKQuote_{bw_token}.csv"
        pdf_fname = f"NKQuote_{bw_token}.pdf"
    elif dim_token:
        fname = f"NKQuote_{dim_token}.csv"
        pdf_fname = f"NKQuote_{dim_token}.pdf"

    # Gather summary data (shared between CSV and PDF exports)
    summary_data = {
        "exact_lbs": total_exact_lbs,
        "alt_yield": total_alt_yield,
        "total_lbs": total_lbs,
        "mweight": mweight,
        "blended_cwt": blended_cost_cwt,
        "cost_per_m": cost_per_m,
        "est_sheets": est_sheets,
        "order_qty": order_quantity_param,
        "order_qty_cost_cwt": order_qty_cost_cwt,
        "order_qty_cost_per_m": order_qty_cost_per_m,
        "freight_total": freight_cost if freight_cost and freight_cost > 0 else None,
        "freight_cwt": freight_cwt if freight_cwt > 0 else None,
        "freight_per_m": freight_per_m if freight_per_m > 0 else None,
    }

    # Build CSV: per-row data + summary footer
    csv_body = export_df.to_csv(index=False)
    footer_rows = [
        "",
        "Summary,",
        f"Exact Qty Selected,{total_exact_lbs:,.0f} lbs",
        f"Alt Yield Selected,{total_alt_yield:,.0f} lbs",
        f"Total Estimated Yield,{total_lbs:,.0f} lbs",
        f"Mweight,{mweight:,.0f} lbs" if mweight else "Mweight,—",
        f"Blended Cost / CWT,${blended_cost_cwt:,.2f}",
        f"Cost Per M Sheets,${cost_per_m:,.2f}" if cost_per_m is not None else "Cost Per M Sheets,—",
        f"Estimated Sheets,{est_sheets:,.0f}" if est_sheets is not None else "Estimated Sheets,—",
        (
            f"Order Qty,{_sheets_requested:,} sheets"
            + (f" (≈ {order_quantity_param:,.0f} lbs)" if order_quantity_param else "")
            if (st.session_state.search_params.get("order_qty_unit") == "Sheets" and _sheets_requested)
            else (f"Order Qty,{order_quantity_param:,.0f} lbs" if order_quantity_param else "Order Qty,—")
        ),
    ]
    if freight_cwt > 0:
        footer_rows.extend([
            f"Freight Total,${freight_cost:,.2f}",
            f"Freight / CWT,${freight_cwt:,.2f}",
            f"Freight / M Sheets,${freight_per_m:,.2f}",
        ])
    footer_rows.extend([
        f"Order Qty Cost / CWT,${order_qty_cost_cwt:,.2f}" if order_qty_cost_cwt is not None else "Order Qty Cost / CWT,—",
        f"Order Qty Cost / M Sheets,${order_qty_cost_per_m:,.2f}" if order_qty_cost_per_m is not None else "Order Qty Cost / M Sheets,—",
    ])
    # Append selected per-roll detail to the CSV (after the aggregated rows, before the summary)
    detail_section = ""
    detail_cols_sheets = ["LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                          "SheetWidth", "SheetLength", "Mill", "Warehouse", "QtyOnHand", "CostPerCWT"]
    detail_cols_rolls = ["LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                         "Roll_Width", "Diameter", "Mill", "Warehouse", "QtyOnHand", "CostPerCWT"]
    if not detail_sheets_df.empty:
        cols_present = [c for c in detail_cols_sheets if c in detail_sheets_df.columns]
        detail_section += "\nSelected Alt-Sheet Rolls (detail)\n"
        detail_section += detail_sheets_df[cols_present].to_csv(index=False)
    if not detail_rolls_df.empty:
        cols_present = [c for c in detail_cols_rolls if c in detail_rolls_df.columns]
        detail_section += "\nSelected Alt Rolls (detail)\n"
        detail_section += detail_rolls_df[cols_present].to_csv(index=False)

    csv_with_summary = csv_body + detail_section + "\n".join(footer_rows) + "\n"

    # Export buttons side by side
    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 3])

    with btn_col1:
        st.download_button(
            "💾 Export to CSV",
            csv_with_summary.encode("utf-8"),
            file_name=fname,
            mime="text/csv",
        )

    with btn_col2:
        pdf_bytes = generate_quote_pdf(
            st.session_state.search_params,
            selected_exact,
            selected_alt_sheets,
            selected_alt_rolls,
            summary_data,
            detail_sheets=detail_sheets_df,
            detail_rolls=detail_rolls_df,
        )

        st.download_button(
            "📄 Export to PDF",
            pdf_bytes,
            file_name=pdf_fname,
            mime="application/pdf",
        )
else:
    st.caption("Select rows above to enable export.")

# =========================================================
# DISPLAY: RESERVED INVENTORY
# =========================================================
if reserve_inv_df is not None and not reserve_inv_df.empty and requested_width is not None:
    sp = st.session_state.search_params
    req_width = float(sp.get("sheet_width_input", 0) or 0)
    req_length = float(sp.get("sheet_length_input", 0) or 0)
    mw_pct = sp.get("max_waste_pct", 10)

    # Filter to reservations older than 30 days
    cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=RESERVED_MIN_AGE_DAYS)
    ri = reserve_inv_df[reserve_inv_df["ReserveDate"] <= cutoff_date].copy()

    # Apply same search filters as main inventory
    if "WarehouseGroup" in ri.columns and sp.get("warehouse_group", "All") != "All":
        ri = ri[ri["WarehouseGroup"] == sp["warehouse_group"]]

    sp_pg = sp.get("product_group", "All")
    if sp_pg != "All":
        if "ProductGroupID" in ri.columns:
            ri = ri[ri["ProductGroupID"] == sp_pg]
        elif "ProductGroup" in ri.columns:
            ri = ri[ri["ProductGroup"] == sp_pg]

    if "GradeName" in ri.columns and sp.get("grade_names"):
        ri = ri[ri["GradeName"].isin(sp["grade_names"])]

    if "BasisWt" in ri.columns and sp.get("basis_weights"):
        ri = ri[ri["BasisWt"].isin(sp["basis_weights"])]

    sp_cal = sp.get("calipers")
    if "Caliper" in ri.columns and sp_cal:
        caliper_floats = [float(c) for c in sp_cal]
        ri = ri[pd.to_numeric(ri["Caliper"], errors="coerce").round(4).isin(caliper_floats)]

    # --- Match sheets: exact + alternative (by area waste) ---
    ri_sheets = pd.DataFrame()
    if req_width > 0 and req_length > 0:
        if "SheetWidth" in ri.columns and "SheetLength" in ri.columns:
            ri_s = ri.dropna(subset=["SheetWidth", "SheetLength"]).copy()
            ri_s = ri_s[(ri_s["SheetWidth"] > 0) & (ri_s["SheetLength"] > 0)]

            # Exact sheet matches
            ri_s_exact = ri_s[
                (ri_s["SheetWidth"].round(2) == round(req_width, 2)) &
                (ri_s["SheetLength"].round(2) == round(req_length, 2))
            ].copy()
            ri_s_exact["Splits"] = 1
            ri_s_exact["Waste_Pct"] = 0.0
            ri_s_exact["MatchType"] = "Sheet"

            # Alternative sheets (larger in both dimensions)
            ri_s_alt = ri_s[
                (ri_s["SheetWidth"] >= req_width) &
                (ri_s["SheetLength"] >= req_length) &
                ~((ri_s["SheetWidth"].round(2) == round(req_width, 2)) &
                  (ri_s["SheetLength"].round(2) == round(req_length, 2)))
            ].copy()
            if not ri_s_alt.empty:
                ri_s_alt["Requested_Area"] = req_width * req_length
                ri_s_alt["Actual_Area"] = ri_s_alt["SheetWidth"] * ri_s_alt["SheetLength"]
                ri_s_alt["Waste_Pct"] = ((ri_s_alt["Actual_Area"] - ri_s_alt["Requested_Area"]) / ri_s_alt["Actual_Area"]) * 100.0
                ri_s_alt["Splits"] = 1
                ri_s_alt = ri_s_alt[ri_s_alt["Waste_Pct"] <= mw_pct]
                ri_s_alt["MatchType"] = "Sheet"

            ri_sheets = pd.concat([ri_s_exact, ri_s_alt], ignore_index=True)

    # --- Match rolls: exact + alternative (by width splits) ---
    ri_rolls = pd.DataFrame()
    if req_width > 0 and "Roll_Width" in ri.columns:
        ri_r = ri.dropna(subset=["Roll_Width"]).copy()
        ri_r = ri_r[ri_r["Roll_Width"] > 0]

        # Exact roll width
        ri_r_exact = ri_r[ri_r["Roll_Width"].round(2) == round(req_width, 2)].copy()
        ri_r_exact["Splits"] = 1
        ri_r_exact["Waste_Pct"] = 0.0
        ri_r_exact["MatchType"] = "Roll"

        # Alternative rolls (wider, within waste tolerance)
        ri_r_alt = ri_r[ri_r["Roll_Width"] > req_width].copy()
        if not ri_r_alt.empty:
            ri_r_alt["Splits"] = (ri_r_alt["Roll_Width"] / req_width).astype(int)
            ri_r_alt["Waste_Inches"] = ri_r_alt["Roll_Width"] - (ri_r_alt["Splits"] * req_width)
            ri_r_alt["Waste_Pct"] = (ri_r_alt["Waste_Inches"] / ri_r_alt["Roll_Width"]) * 100.0
            ri_r_alt = ri_r_alt[ri_r_alt["Waste_Pct"] <= mw_pct]
            ri_r_alt["MatchType"] = "Roll"

        ri_rolls = pd.concat([ri_r_exact, ri_r_alt], ignore_index=True)

    ri_combined = pd.concat([ri_sheets, ri_rolls], ignore_index=True)

    if not ri_combined.empty:
        ri_combined["DaysReserved"] = (pd.Timestamp.now() - ri_combined["ReserveDate"]).dt.days

        # Group by key fields
        ri_group_cols = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "SheetWidth", "SheetLength",
                         "Mill", "Brand", "MatchType", "ResSONum", "ReserveSalesRep", "ResCust", "Warehouse"]
        available_grp = [c for c in ri_group_cols if c in ri_combined.columns]

        ri_grouped = ri_combined.groupby(available_grp, dropna=False).agg(
            QtyOnHand=("QtyOnHand", "sum"),
            Units=("Units", "sum"),
            Splits=("Splits", "first"),
            Waste_Pct=("Waste_Pct", "first"),
            ReserveDate=("ReserveDate", "min"),
            DaysReserved=("DaysReserved", "max"),
        ).reset_index()

        ri_grouped = ri_grouped.sort_values("DaysReserved", ascending=False)

        st.markdown("---")
        st.subheader(f"🔒 Reserved Inventory (> {RESERVED_MIN_AGE_DAYS} days)")
        if st.session_state.search_params.get("include_reserved"):
            st.caption(
                "These rolls/sheets are also selectable in the results above, flagged 🔒 "
                "with their customer."
            )
        else:
            st.caption(
                "Reference only. Tick **Include reserved inventory** in the search form "
                "to quote against these."
            )

        # Build display dataframe
        ri_display = ri_grouped[[]].copy()
        ri_display["Grade"] = ri_grouped.get("GradeName", "")
        ri_display["BasisWt"] = ri_grouped["BasisWt"]
        ri_display["Caliper"] = ri_grouped["Caliper"]
        # Width: Roll_Width for rolls, SheetWidth for sheets
        ri_display["Width"] = ri_grouped.apply(
            lambda r: r.get("Roll_Width") if r.get("MatchType") == "Roll" else r.get("SheetWidth"), axis=1
        )
        ri_display["Length"] = ri_grouped.apply(
            lambda r: r.get("SheetLength") if r.get("MatchType") == "Sheet" else None, axis=1
        )
        ri_display["Type"] = ri_grouped.get("MatchType", "")
        ri_display["Mill"] = ri_grouped.get("Mill", "")
        ri_display["Brand"] = ri_grouped.get("Brand", "")
        ri_display["Qty"] = ri_grouped["QtyOnHand"]
        ri_display["#Rolls"] = ri_grouped["Units"]
        ri_display["Splits"] = ri_grouped["Splits"]
        ri_display["Waste%"] = ri_grouped["Waste_Pct"]
        ri_display["Reserved"] = ri_grouped["ReserveDate"]
        ri_display["Days"] = ri_grouped["DaysReserved"]
        ri_display["SO#"] = ri_grouped.get("ResSONum", "")
        ri_display["Sales Rep"] = ri_grouped.get("ReserveSalesRep", "")
        ri_display["Customer"] = ri_grouped.get("ResCust", "")
        ri_display["Warehouse"] = ri_grouped.get("Warehouse", "")

        ri_col_config = {
            "Grade": st.column_config.TextColumn("Grade", width="medium"),
            "BasisWt": st.column_config.NumberColumn("BasisWt", format="%d", width="small"),
            "Caliper": st.column_config.NumberColumn("Caliper", format="%.3f", width="small"),
            "Width": st.column_config.NumberColumn("Width", format='%.2f"', width="small"),
            "Length": st.column_config.NumberColumn("Length", format='%.2f"', width="small"),
            "Type": st.column_config.TextColumn("Type", width="small"),
            "Mill": st.column_config.TextColumn("Mill", width="medium"),
            "Brand": st.column_config.TextColumn("Brand", width="medium"),
            "Qty": st.column_config.NumberColumn("Qty", format="%.0f", width="small"),
            "#Rolls": st.column_config.NumberColumn("#Rolls", format="%d", width="small"),
            "Splits": st.column_config.NumberColumn("Splits", format="%d", width="small"),
            "Waste%": st.column_config.NumberColumn("Waste%", format="%.1f%%", width="small"),
            "Reserved": st.column_config.DateColumn("Reserved", format="YYYY-MM-DD", width="medium"),
            "Days": st.column_config.NumberColumn("Days", format="%d", width="small"),
            "SO#": st.column_config.TextColumn("SO#", width="medium"),
            "Sales Rep": st.column_config.TextColumn("Sales Rep", width="medium"),
            "Customer": st.column_config.TextColumn("Customer", width="medium"),
            "Warehouse": st.column_config.TextColumn("Warehouse", width="medium"),
        }

        st.dataframe(ri_display, use_container_width=False, hide_index=True, column_config=ri_col_config)

# =========================================================
# RECENT SALES ORDERS
# =========================================================
if po_detail_df is not None and not po_detail_df.empty:
    # Collect unique GradeIDs from all search results
    grade_ids = set()

    # From alternative_rolls: GradeID is directly available
    if not alternative_rolls.empty and "GradeID" in alternative_rolls.columns:
        grade_ids.update(
            alternative_rolls["GradeID"].dropna().astype(str).str.strip().tolist()
        )

    # From alternative_sheets: GradeID is directly available
    if not alternative_sheets.empty and "GradeID" in alternative_sheets.columns:
        grade_ids.update(
            alternative_sheets["GradeID"].dropna().astype(str).str.strip().tolist()
        )

    # From exact_matches: look up GradeID via GradeName from Grade table
    if not exact_matches.empty and "GradeName" in exact_matches.columns:
        if "GradeID" in exact_matches.columns:
            grade_ids.update(
                exact_matches["GradeID"].dropna().astype(str).str.strip().tolist()
            )
        elif grade_df is not None and "Description" in grade_df.columns:
            for gn in exact_matches["GradeName"].dropna().unique():
                match = grade_df[grade_df["Description"].astype(str).str.strip() == str(gn).strip()]
                if not match.empty:
                    grade_ids.update(
                        match["GradeID"].dropna().astype(str).str.strip().tolist()
                    )

    if grade_ids and "GradeID" in po_detail_df.columns:
        po_filtered = po_detail_df[
            po_detail_df["GradeID"].astype(str).str.strip().isin(grade_ids)
        ].copy()

        if not po_filtered.empty:
            po_filtered = po_filtered.sort_values("PODate", ascending=False)

            display_cols = ["PODate", "Customer", "GradeName", "BasisWt", "Caliper", "SheetSize", "MWeight", "WeightLB", "PriceCWT"]
            available = [c for c in display_cols if c in po_filtered.columns]
            po_display = po_filtered[available].copy()

            rename_map = {
                "PODate": "Date",
                "Customer": "Customer Name",
                "GradeName": "Grade Name",
                "BasisWt": "Basis Wt",
                "Caliper": "Caliper",
                "SheetSize": "Sheet Size",
                "WeightLB": "Order Qty (lbs)",
                "MWeight": "MWeight",
                "PriceCWT": "Price/CWT",
            }
            po_display = po_display.rename(columns={c: rename_map[c] for c in available if c in rename_map})

            st.markdown("---")
            st.subheader("📋 Recent Sales Orders")

            col_config = {}
            if "Date" in po_display.columns:
                col_config["Date"] = st.column_config.DateColumn("Date", format="YYYY-MM-DD")
            if "Order Qty (lbs)" in po_display.columns:
                col_config["Order Qty (lbs)"] = st.column_config.NumberColumn("Order Qty (lbs)", format="%.0f")
            if "MWeight" in po_display.columns:
                col_config["MWeight"] = st.column_config.NumberColumn("MWeight", format="%.0f")
            if "Price/CWT" in po_display.columns:
                col_config["Price/CWT"] = st.column_config.NumberColumn("Price/CWT", format="$%.2f")
            if "Basis Wt" in po_display.columns:
                col_config["Basis Wt"] = st.column_config.NumberColumn("Basis Wt", format="%d")
            if "Caliper" in po_display.columns:
                col_config["Caliper"] = st.column_config.NumberColumn("Caliper", format="%.3f")

            st.dataframe(po_display, use_container_width=True, hide_index=True, column_config=col_config)
        else:
            st.markdown("---")
            st.subheader("📋 Recent Sales Orders")
            st.info("No recent sales order data found for the matching grades.")
    else:
        st.markdown("---")
        st.subheader("📋 Recent Sales Orders")
        st.info("No recent sales order data found for the matching grades.")
