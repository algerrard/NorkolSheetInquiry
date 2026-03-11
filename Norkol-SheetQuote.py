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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
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
        grade_df["GradeID"] = grade_df["GradeID"].astype(str).str.strip()
        grade_df["ProductGroupID"] = grade_df["ProductGroupID"].astype(str).str.strip()
        grade_df["GSM"] = pd.to_numeric(grade_df["GSM"], errors="coerce")
        grade_df["Area(IN)"] = pd.to_numeric(grade_df["Area(IN)"], errors="coerce")

        # PaperInformation (ProductGroupID + GSM_Factor -> RW_RunAdjust, SHT_RunAdjust, NumShtrRolls, etc.)
        paper_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=PAPER_INFO_BLOB)
        paper_csv = paper_client.download_blob().readall().decode("utf-8")
        paper_df = pd.read_csv(StringIO(paper_csv))
        paper_df["ProductGroupID"] = paper_df["ProductGroupID"].astype(str).str.strip()
        paper_df["GSM_Factor"] = pd.to_numeric(paper_df["GSM_Factor"], errors="coerce")

        # MachineInfo
        machine_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=MACHINE_INFO_BLOB)
        machine_csv = machine_client.download_blob().readall().decode("utf-8")
        machine_df = pd.read_csv(StringIO(machine_csv))

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
        return order_size_adj_df
    except Exception as e:
        st.warning(f"Could not load order size adjustments: {str(e)}")
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

        # Lbs/Hour = BasisWt/(Area*500) * (CutWidth * NumCuts * NumShtrRolls) * (AvgSpeed * 12) * 60
        lbs_per_hour = (
            (basis_lb / (area_in * 500.0))
            * (float(requested_width) * splits * num_shtr_rolls)
            * (avg_speed * 12.0)
            * 60.0
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

        conv_cwt = (
            (total_cost / process_weight) * 100.0
            if process_weight and process_weight > 0
            else None
        )

        # Apply OrderQty surcharge from order size adjustments
        if conv_cwt is not None and order_quantity is not None and order_size_adj_df is not None:
            order_qty_pct = get_order_size_pct(order_size_adj_df, equip_type, "OrderQty", order_quantity)
            conv_cwt *= (1 + order_qty_pct)

        # Minimum 1 hour charge: if total cost after all surcharges is below
        # the hourly rate, bump conv_cwt up to the 1-hour minimum
        min_charge_applied = False
        if conv_cwt is not None and process_weight and process_weight > 0:
            min_cost = hourly_rate  # 1 hour minimum
            final_total_cost = (conv_cwt / 100.0) * process_weight
            if final_total_cost < min_cost:
                conv_cwt = (min_cost / process_weight) * 100.0
                min_charge_applied = True

        return pd.Series(
            {
                "LbsPerHour": lbs_per_hour,
                "ConvHrs": total_hours,
                "ConvertingCostPerCWT": conv_cwt,
                "MinChargeApplied": min_charge_applied,
            }
        )

    except Exception:
        return pd.Series(
            {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None, "MinChargeApplied": False}
        )


# =========================================================
# PDF REPORT GENERATION
# =========================================================
def generate_quote_pdf(search_params, selected_exact, selected_alt_sheets, selected_alt_rolls, summary_data):
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
    
    param_data = [
        ["Warehouse Group:", str(search_params.get("warehouse_group") or "All")],
        ["Product Group:", str(search_params.get("product_group") or "All")],
        ["Grade:", ", ".join(str(g) for g in grade_list) if grade_list else "All"],
        ["Basis Weight:", ", ".join(str(b) for b in basis_wt_list) if basis_wt_list else "All"],
        ["Caliper:", ", ".join(str(c) for c in caliper_list) if caliper_list else "All"],
        ["Sheet Width:", f"{search_params.get('sheet_width_input')}\"" if search_params.get("sheet_width_input") else "Not specified"],
        ["Sheet Length:", f"{search_params.get('sheet_length_input')}\"" if search_params.get("sheet_length_input") else "Not specified"],
        ["Max Waste %:", f"{search_params.get('max_waste_pct')}%" if search_params.get("max_waste_pct") is not None else "Not specified"],
        ["Order Quantity:", f"{search_params.get('order_quantity'):,} lbs" if search_params.get("order_quantity") else "Not specified"],
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
    
    summary_table_data = [
        ["Exact Qty Selected:", f"{exact_lbs:,.0f} lbs"],
        ["Alt Yield Selected:", f"{alt_yield:,.0f} lbs"],
        ["Total Usable Weight:", f"{total_lbs:,.0f} lbs"],
        ["Mweight:", f"{mweight:,.0f} lbs" if mweight else "—"],
        ["Blended Cost / CWT:", f"${blended_cwt:,.2f}"],
        ["Cost Per M Sheets:", f"${cost_per_m:,.2f}" if cost_per_m else "—"],
        ["Estimated Sheets:", f"{est_sheets:,.0f}" if est_sheets else "—"],
    ]
    
    summary_table = Table(summary_table_data, colWidths=[2*inch, 2*inch])
    summary_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    story.append(summary_table)
    
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
if df is None:
    st.stop()

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
with st.form("search_form"):
    col1, col2 = st.columns(2)

    with col1:
        wh_opts = (
            ["All"] + sorted(df["WarehouseGroup"].dropna().unique().tolist())
            if "WarehouseGroup" in df.columns
            else ["All"]
        )
        warehouse_group = st.selectbox("Warehouse Group", wh_opts)

        if "ProductGroupID" in df.columns:
            pg_opts = ["All"] + sorted(df["ProductGroupID"].dropna().unique().tolist())
            product_group = st.selectbox("Product Group", pg_opts)
        elif "ProductGroup" in df.columns:
            pg_opts = ["All"] + sorted(df["ProductGroup"].dropna().unique().tolist())
            product_group = st.selectbox("Product Group", pg_opts)
        else:
            product_group = "All"

        gn_opts = (
            sorted(df["GradeName"].dropna().unique().tolist())
            if "GradeName" in df.columns
            else []
        )
        grade_names = st.multiselect("Grade Name(s)", gn_opts, placeholder="All grades (leave empty)")

        bw_opts = (
            sorted([x for x in df["BasisWt"].dropna().unique().tolist()])
            if "BasisWt" in df.columns
            else []
        )
        basis_weights = st.multiselect("Basis Weight(s)", bw_opts, placeholder="All weights (leave empty)")

        if "Caliper" in df.columns:
            caliper_values = pd.to_numeric(df["Caliper"], errors="coerce").dropna().unique()
            cal_opts = [f"{x:.4f}" for x in sorted(caliper_values)]
            calipers = st.multiselect("Caliper(s)", cal_opts, placeholder="All calipers (leave empty)")
        else:
            calipers = []

    with col2:
        sheet_width_input = st.text_input("Sheet Width Needed", placeholder='e.g., 48 or 48.5')
        sheet_length_input = st.text_input("Sheet Length Needed", placeholder='e.g., 36 or 36.5')
        max_waste_pct = st.number_input(
            "Max Waste % (for alternatives)",
            min_value=0.0,
            max_value=100.0,
            value=10.0,
            step=1.0,
        )
        order_quantity = st.number_input(
            "Order Quantity (lbs) *",
            min_value=0,
            value=0,
        )

    c1, c2 = st.columns([1, 3])
    with c1:
        search_btn = st.form_submit_button("🔍 Search", use_container_width=True)
    with c2:
        reset_btn = st.form_submit_button("🔄 Reset", use_container_width=True)

if reset_btn:
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_sheets_idx = set()
    st.session_state.sel_alt_rolls_idx = set()
    st.session_state.search_params = {}
    st.rerun()


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

    if not order_quantity:
        st.error("Order quantity in lbs must be provided")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, pd.DataFrame(), pd.DataFrame()

    filtered = df.copy()

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

        # Rolls must be >= requested width to cut sheets
        suitable_rolls = roll_data[roll_data["Roll_Width"] >= requested_width].copy()
        
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
        
        agg_dict_ex = {"QtyOnHand": "sum"}
        if inv_col and inv_col in ex.columns:
            agg_dict_ex[inv_col] = "sum"
        if "GradeID" in ex.columns:
            agg_dict_ex["GradeID"] = "first"
        if "BasisWtUOM" in ex.columns:
            agg_dict_ex["BasisWtUOM"] = "first"

        exact_matches = ex.groupby(group_cols_exact, as_index=False).agg(agg_dict_ex)

        # AvgCost = (sum inv) / (sum qty) * 100
        if inv_col and inv_col in exact_matches.columns and "QtyOnHand" in exact_matches.columns:
            exact_matches["AvgCost"] = (
                exact_matches[inv_col] / exact_matches["QtyOnHand"]
            ) * 100.0

        # Drop inventory value column from display
        if inv_col and inv_col in exact_matches.columns:
            exact_matches = exact_matches.drop(columns=[inv_col], errors="ignore")

    # =========================================================
    # PROCESS ALTERNATIVE SHEETS
    # =========================================================
    alternative_sheets = pd.DataFrame()
    
    if not alt_sheets.empty:
        al_sh = alt_sheets.copy()
        if "Caliper" in al_sh.columns:
            al_sh["Caliper"] = pd.to_numeric(al_sh["Caliper"], errors="coerce")
        
        # Yield = QtyOnHand * (1 - Waste_Pct/100)
        if "QtyOnHand" in al_sh.columns and "Waste_Pct" in al_sh.columns:
            al_sh["Yield"] = al_sh["QtyOnHand"] * (1 - al_sh["Waste_Pct"] / 100.0)

        # CRITICAL: Convert inventory value to numeric BEFORE groupby
        if inv_col and inv_col in al_sh.columns:
            al_sh[inv_col] = al_sh[inv_col].replace({',': ''}, regex=True)
            al_sh[inv_col] = pd.to_numeric(al_sh[inv_col], errors="coerce")

        # Group by columns for sheets
        group_cols_sheets = ["GradeName", "BasisWt", "Caliper", width_col, length_col, "Mill", "Brand"]
        group_cols_sheets = [c for c in group_cols_sheets if c in al_sh.columns]
        
        agg_sheets = {
            "QtyOnHand": "sum",
            "Yield": "sum",
            "Splits": "first",
            "Waste_Pct": "first",
        }

        if "Units" in al_sh.columns:
            agg_sheets["Units"] = "sum"
        if inv_col and inv_col in al_sh.columns:
            agg_sheets[inv_col] = "sum"
        if "GradeID" in al_sh.columns:
            agg_sheets["GradeID"] = "first"
        if "BasisWtUOM" in al_sh.columns:
            agg_sheets["BasisWtUOM"] = "first"

        alternative_sheets = al_sh.groupby(group_cols_sheets, as_index=False).agg(agg_sheets)

        # Calculate costs
        if inv_col and inv_col in alternative_sheets.columns and "Yield" in alternative_sheets.columns:
            alternative_sheets["NetAvgCost"] = (
                alternative_sheets[inv_col] / alternative_sheets["Yield"]
            ) * 100.0
            if "QtyOnHand" in alternative_sheets.columns:
                alternative_sheets["AvgCost"] = (
                    alternative_sheets[inv_col] / alternative_sheets["QtyOnHand"]
                ) * 100.0
        else:
            alternative_sheets["AvgCost"] = np.nan
            alternative_sheets["NetAvgCost"] = np.nan

        # Apply RunWaste from order size adjustments (always Sheeter for sheet operations)
        if order_quantity is not None and order_size_adj_df is not None:
            run_waste_pct = get_order_size_pct(order_size_adj_df, "Sheeter", "RunWaste", order_quantity)
            alternative_sheets["RunWastePct"] = run_waste_pct

            if "Yield" in alternative_sheets.columns:
                alternative_sheets["Yield"] = (
                    alternative_sheets["Yield"] * (1 - run_waste_pct)
                )
            if "NetAvgCost" in alternative_sheets.columns:
                alternative_sheets["NetAvgCost"] = (
                    alternative_sheets["NetAvgCost"] * (1 + run_waste_pct)
                )

        # Add Trimmer converting cost for sheets
        if machine_info_df is not None and "EquipType" in machine_info_df.columns:
            trimmer_row = machine_info_df[machine_info_df["EquipType"].astype(str).str.strip() == "Trimmer"]
            if len(trimmer_row) > 0:
                # Get PerCWTRate from the Trimmer row
                per_cwt_rate = trimmer_row.iloc[0].get("PerCWTRate", None)
                if per_cwt_rate is not None:
                    # Clean and convert to float (handle $, commas, etc.)
                    if isinstance(per_cwt_rate, str):
                        per_cwt_rate = per_cwt_rate.replace('$', '').replace(',', '').strip()
                    try:
                        per_cwt_rate = float(per_cwt_rate)
                        # Apply OrderQty surcharge to converting rate
                        if order_quantity is not None and order_size_adj_df is not None:
                            order_qty_pct = get_order_size_pct(order_size_adj_df, "Sheeter", "OrderQty", order_quantity)
                            per_cwt_rate *= (1 + order_qty_pct)
                        alternative_sheets["ConvertingCostPerCWT"] = per_cwt_rate
                        # FinalCostCWT = NetAvgCost + ConvertingCostPerCWT
                        if "NetAvgCost" in alternative_sheets.columns:
                            alternative_sheets["FinalCostCWT"] = (
                                alternative_sheets["NetAvgCost"].fillna(0.0) + per_cwt_rate
                            )
                    except (ValueError, TypeError):
                        alternative_sheets["ConvertingCostPerCWT"] = np.nan
                        alternative_sheets["FinalCostCWT"] = np.nan
                else:
                    alternative_sheets["ConvertingCostPerCWT"] = np.nan
                    alternative_sheets["FinalCostCWT"] = np.nan
            else:
                alternative_sheets["ConvertingCostPerCWT"] = np.nan
                alternative_sheets["FinalCostCWT"] = np.nan
        else:
            alternative_sheets["ConvertingCostPerCWT"] = np.nan
            alternative_sheets["FinalCostCWT"] = np.nan

        # Drop inventory value column
        if inv_col and inv_col in alternative_sheets.columns:
            alternative_sheets = alternative_sheets.drop(columns=[inv_col], errors="ignore")

    # =========================================================
    # PROCESS ALTERNATIVE ROLLS
    # =========================================================
    alternative_rolls = pd.DataFrame()
    
    if not roll_results.empty:
        al_rl = roll_results.copy()
        if "Caliper" in al_rl.columns:
            al_rl["Caliper"] = pd.to_numeric(al_rl["Caliper"], errors="coerce")
        
        # Yield = QtyOnHand * (1 - Waste_Pct/100)
        if "QtyOnHand" in al_rl.columns and "Waste_Pct" in al_rl.columns:
            al_rl["Yield"] = al_rl["QtyOnHand"] * (1 - al_rl["Waste_Pct"] / 100.0)

        # CRITICAL: Convert inventory value to numeric BEFORE groupby
        if inv_col and inv_col in al_rl.columns:
            al_rl[inv_col] = al_rl[inv_col].replace({',': ''}, regex=True)
            al_rl[inv_col] = pd.to_numeric(al_rl[inv_col], errors="coerce")

        # Group by columns for rolls
        group_cols_rolls = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "Mill", "Brand"]
        group_cols_rolls = [c for c in group_cols_rolls if c in al_rl.columns]
        
        agg_rolls = {
            "QtyOnHand": "sum",
            "Yield": "sum",
            "Splits": "first",
            "Waste_Pct": "first",
        }

        if "Units" in al_rl.columns:
            agg_rolls["Units"] = "sum"
        if inv_col and inv_col in al_rl.columns:
            agg_rolls[inv_col] = "sum"
        if "GradeID" in al_rl.columns:
            agg_rolls["GradeID"] = "first"
        if "BasisWtUOM" in al_rl.columns:
            agg_rolls["BasisWtUOM"] = "first"
        if "ProductCategoryID" in al_rl.columns:
            agg_rolls["ProductCategoryID"] = "first"

        alternative_rolls = al_rl.groupby(group_cols_rolls, as_index=False).agg(agg_rolls)

        # Calculate costs
        if inv_col and inv_col in alternative_rolls.columns and "Yield" in alternative_rolls.columns:
            alternative_rolls["NetAvgCost"] = (
                alternative_rolls[inv_col] / alternative_rolls["Yield"]
            ) * 100.0
            if "QtyOnHand" in alternative_rolls.columns:
                alternative_rolls["AvgCost"] = (
                    alternative_rolls[inv_col] / alternative_rolls["QtyOnHand"]
                ) * 100.0
        else:
            alternative_rolls["AvgCost"] = np.nan
            alternative_rolls["NetAvgCost"] = np.nan

        # Apply RunWaste from order size adjustments (always Sheeter for sheet operations)
        if order_quantity is not None and order_size_adj_df is not None:
            run_waste_pct = get_order_size_pct(order_size_adj_df, "Sheeter", "RunWaste", order_quantity)
            alternative_rolls["RunWastePct"] = run_waste_pct

            if "Yield" in alternative_rolls.columns:
                alternative_rolls["Yield"] = (
                    alternative_rolls["Yield"] * (1 - run_waste_pct)
                )
            if "NetAvgCost" in alternative_rolls.columns:
                alternative_rolls["NetAvgCost"] = (
                    alternative_rolls["NetAvgCost"] * (1 + run_waste_pct)
                )

        # Conversion metrics for rolls
        if (
            not alternative_rolls.empty
            and grade_df is not None
            and paper_info_df is not None
            and machine_info_df is not None
        ):
            conv_series = alternative_rolls.apply(
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

            alternative_rolls = pd.concat(
                [alternative_rolls.reset_index(drop=True), conv_final], axis=1
            )

        # FinalCostCWT = NetAvgCost + ConvertingCostPerCWT
        if "NetAvgCost" in alternative_rolls.columns and "ConvertingCostPerCWT" in alternative_rolls.columns:
            alternative_rolls["FinalCostCWT"] = (
                alternative_rolls["NetAvgCost"].fillna(0.0)
                + alternative_rolls["ConvertingCostPerCWT"].fillna(0.0)
            )

        # Drop inventory value column
        if inv_col and inv_col in alternative_rolls.columns:
            alternative_rolls = alternative_rolls.drop(columns=[inv_col], errors="ignore")

    return exact_matches, alternative_sheets, alternative_rolls, requested_width, alt_sheets, roll_results


# =========================================================
# EXECUTE SEARCH (Unified)
# =========================================================
if search_btn:
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
        "order_quantity": order_quantity,
    }
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_sheets_idx = set()
    st.session_state.sel_alt_rolls_idx = set()

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
if not exact_matches.empty:
    # 10 columns: checkbox, grade, basis, caliper, sheet width, sheet length, mill, brand, qty, cost
    header_cols = st.columns([0.5, 1.6, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1])
    with header_cols[0]:
        st.markdown("**☑**")
    with header_cols[1]:
        st.markdown("**Grade**")
    with header_cols[2]:
        st.markdown("**BasisWt**")
    with header_cols[3]:
        st.markdown("**Caliper**")
    with header_cols[4]:
        st.markdown("**SheetWidth**")
    with header_cols[5]:
        st.markdown("**SheetLength**")
    with header_cols[6]:
        st.markdown("**Mill**")
    with header_cols[7]:
        st.markdown("**Brand**")
    with header_cols[8]:
        st.markdown("**QtyOnHand**")
    with header_cols[9]:
        st.markdown("**AvgCost**")
    st.markdown("---")

    for idx, row in exact_matches.iterrows():
        cols = st.columns([0.5, 1.6, 0.9, 0.9, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1])
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

    H_sh = st.columns(sheet_ratios)
    sheet_headers = [
        "☑", "Grade", "BasisWt", "Caliper", "SheetWidth", "SheetLength",
        "Mill", "Brand", "Qty", "Waste%", "RunW%", "Yield", "NetAvgCost", "Conv$/CWT", "FinalCost/CWT"
    ]

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

    with st.expander("📋 Alternative Sheets Details — Underlying Inventory"):
        sel_sh_idx = sorted(list(st.session_state.sel_alt_sheets_idx))
        if sel_sh_idx and not alt_sheets_raw.empty:
            selected_groups = alternative_sheets.iloc[sel_sh_idx]
            # Determine sheet width/length column names dynamically
            sh_group_keys = ["GradeName", "BasisWt", "Caliper", "Mill", "Brand"]
            for col in ["SheetWidth", "Sheet_Width", "Width"]:
                if col in alt_sheets_raw.columns and col in selected_groups.columns:
                    sh_group_keys.append(col)
                    break
            for col in ["SheetLength", "Sheet_Length", "Length"]:
                if col in alt_sheets_raw.columns and col in selected_groups.columns:
                    sh_group_keys.append(col)
                    break
            available_keys = [k for k in sh_group_keys if k in alt_sheets_raw.columns and k in selected_groups.columns]

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

                inv_display_cols = [
                    "COID", "LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "SheetWidth", "SheetLength", "Condition", "Mill", "Brand",
                    "Warehouse", "QtyOnHand", "Units", "CostPerCWT",
                ]
                available_display = [c for c in inv_display_cols if c in detail_rows.columns]
                detail_display = detail_rows[available_display].copy()

                col_config = {}
                if "QtyOnHand" in detail_display.columns:
                    col_config["QtyOnHand"] = st.column_config.NumberColumn("QtyOnHand", format="%.0f")
                if "BasisWt" in detail_display.columns:
                    col_config["BasisWt"] = st.column_config.NumberColumn("BasisWt", format="%d")
                if "Caliper" in detail_display.columns:
                    col_config["Caliper"] = st.column_config.NumberColumn("Caliper", format="%.4f")
                if "CostPerCWT" in detail_display.columns:
                    col_config["CostPerCWT"] = st.column_config.NumberColumn("Cost/CWT", format="$%.2f")

                st.dataframe(detail_display, use_container_width=True, hide_index=True, column_config=col_config)
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
if not alternative_rolls.empty:
    # Ensure required computed columns exist
    for c in ["Yield", "AvgCost", "NetAvgCost", "LbsPerHour", "ConvHrs", "ConvertingCostPerCWT", "FinalCostCWT", "RunWastePct", "MinChargeApplied"]:
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

    H_rl = st.columns(roll_ratios)
    roll_headers = [
        "☑", "Grade", "BasisWt", "Caliper", "RollWidth",
        "Mill", "Brand", "Qty", "Splits", "Waste%", "RunW%", "Yield", "NetAvgCost",
        "Lbs/Hr", "ConvHrs", "Conv$/CWT", "FinalCost/CWT"
    ]

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
            is_min = row.get('MinChargeApplied')
            suffix = " *" if is_min else ""
            st.write(f"${v:.2f}{suffix}" if v is not None else '')
        with C[16]:  # FinalCost/CWT
            v = row.get('FinalCostCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')

    # Footnote if any rows have minimum charge applied
    if alternative_rolls["MinChargeApplied"].any():
        st.caption("* Minimum 1-hour converting charge applied")

    with st.expander("📋 Alternative Rolls Details — Underlying Inventory"):
        sel_rl_idx = sorted(list(st.session_state.sel_alt_rolls_idx))
        if sel_rl_idx and not alt_rolls_raw.empty:
            selected_groups = alternative_rolls.iloc[sel_rl_idx]
            group_keys = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "Mill", "Brand"]
            available_keys = [k for k in group_keys if k in alt_rolls_raw.columns and k in selected_groups.columns]

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

                inv_display_cols = [
                    "COID", "LotNo", "RollNo", "GradeName", "BasisWt", "Caliper",
                    "Roll_Width", "Diameter", "Condition", "Mill", "Brand",
                    "Warehouse", "QtyOnHand", "Units", "CostPerCWT",
                ]
                available_display = [c for c in inv_display_cols if c in detail_rows.columns]
                detail_display = detail_rows[available_display].copy()

                col_config = {}
                if "QtyOnHand" in detail_display.columns:
                    col_config["QtyOnHand"] = st.column_config.NumberColumn("QtyOnHand", format="%.0f")
                if "BasisWt" in detail_display.columns:
                    col_config["BasisWt"] = st.column_config.NumberColumn("BasisWt", format="%d")
                if "Caliper" in detail_display.columns:
                    col_config["Caliper"] = st.column_config.NumberColumn("Caliper", format="%.4f")
                if "Roll_Width" in detail_display.columns:
                    col_config["Roll_Width"] = st.column_config.NumberColumn("Roll_Width", format="%.2f")
                if "Diameter" in detail_display.columns:
                    col_config["Diameter"] = st.column_config.NumberColumn("Diameter", format="%.0f")
                if "CostPerCWT" in detail_display.columns:
                    col_config["CostPerCWT"] = st.column_config.NumberColumn("Cost/CWT", format="$%.2f")

                st.dataframe(detail_display, use_container_width=True, hide_index=True, column_config=col_config)
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
selected_alt_sheets = (
    alternative_sheets.iloc[alt_sheets_sel_idx_sorted]
    if (not alternative_sheets.empty and alt_sheets_sel_idx_sorted)
    else pd.DataFrame()
)
selected_alt_rolls = (
    alternative_rolls.iloc[alt_rolls_sel_idx_sorted]
    if (not alternative_rolls.empty and alt_rolls_sel_idx_sorted)
    else pd.DataFrame()
)

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

c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
with c1:
    st.metric("Exact Qty Selected", f"{total_exact_lbs:,.0f} lbs")
with c2:
    st.metric("Alt Yield Selected", f"{total_alt_yield:,.0f} lbs")
with c3:
    st.metric("Total Usable Weight", f"{total_lbs:,.0f} lbs")
with c4:
    st.metric("Mweight", f"{mweight:,.0f} lbs" if mweight is not None else "—")
with c5:
    st.metric("Blended Cost", f"${blended_cost_cwt:,.2f} / CWT")
with c6:
    st.metric("Cost Per M Sheets", f"${cost_per_m:,.2f}" if cost_per_m is not None else "—")
with c7:
    st.metric("Est. Sheets", f"{est_sheets:,.0f}" if est_sheets is not None else "—")

if mweight_error:
    st.error(mweight_error)

if not exact_sel_idx_sorted and not alt_sheets_sel_idx_sorted and not alt_rolls_sel_idx_sorted:
    st.info("No rows selected yet above.")

# CSV Export of selected rows
export_df = pd.concat([selected_exact, selected_alt_sheets, selected_alt_rolls], ignore_index=True) if (
    not selected_exact.empty or not selected_alt_sheets.empty or not selected_alt_rolls.empty
) else pd.DataFrame()

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

    # Export buttons side by side
    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 3])
    
    with btn_col1:
        st.download_button(
            "💾 Export to CSV",
            export_df.to_csv(index=False).encode("utf-8"),
            file_name=fname,
            mime="text/csv",
        )
    
    with btn_col2:
        # Gather summary data for PDF
        summary_data = {
            "exact_lbs": total_exact_lbs,
            "alt_yield": total_alt_yield,
            "total_lbs": total_lbs,
            "mweight": mweight,
            "blended_cwt": blended_cost_cwt,
            "cost_per_m": cost_per_m,
            "est_sheets": est_sheets,
        }
        
        # Generate PDF
        pdf_bytes = generate_quote_pdf(
            st.session_state.search_params,
            selected_exact,
            selected_alt_sheets,
            selected_alt_rolls,
            summary_data
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
