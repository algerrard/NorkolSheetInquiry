import streamlit as st
import pandas as pd
import numpy as np
from azure.storage.blob import BlobServiceClient
from io import StringIO
from datetime import datetime
import os

# =========================================================
# SHEET SEARCH ENHANCEMENT - November 2025
# =========================================================
# Enhanced to support searching for both sheets and rolls:
#
# SHEETS (when both width AND length are provided):
#   - Exact matches: Both dimensions must match exactly
#   - Alternatives: Width >= requested AND Length >= requested
#   - Waste calculation based on total area difference
#
# ROLLS (when only roll width is provided):
#   - Exact matches: Roll width must match exactly  
#   - Alternatives: Roll width >= requested (can be slit)
#   - Waste calculation based on width after splitting
#
# This allows the app to handle both sheet stock and roll stock
# inventory with appropriate matching logic for each type.
# =========================================================

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(page_title="Norkol Sheet Stock Search", page_icon="📦", layout="wide")

# Session state: selections & search params
if "sel_exact_idx" not in st.session_state:
    st.session_state.sel_exact_idx = set()
if "sel_alt_idx" not in st.session_state:
    st.session_state.sel_alt_idx = set()
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

        # PaperInformation
        paper_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=PAPER_INFO_BLOB)
        paper_csv = paper_client.download_blob().readall().decode("utf-8")
        paper_df = pd.read_csv(StringIO(paper_csv))
        if "GradeID" in paper_df.columns:
            paper_df["GradeID"] = (
                paper_df["GradeID"].astype(str).str.strip().str.replace(r"\s+", "", regex=True)
            )

        # MachineInfo
        machine_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=MACHINE_INFO_BLOB)
        machine_csv = machine_client.download_blob().readall().decode("utf-8")
        machine_df = pd.read_csv(StringIO(machine_csv))

        return paper_df, machine_df
    except Exception as e:
        st.warning(f"Could not load supplementary data: {str(e)}")
        return None, None


# =========================================================
# CONVERTING COST CALCULATION
# =========================================================
def calculate_conversion_cost(row, requested_width, paper_info_df, machine_info_df):
    """
    Calculate converting cost metrics for a grouped alternative row.
    Uses Yield (preferred) or QtyOnHand as processing weight.
    Returns: LbsPerHour, ConvHrs, ConvertingCostPerCWT
    """
    try:
        grade_id = str(row.get("GradeID", "")).strip()
        grade_name = str(row.get("GradeName", "")).lower()
        equip_type = "Sheeter" if "sht" in grade_name else "Rewinder"

        # Lookup paper info
        paper_row = None
        if (
            paper_info_df is not None
            and "GradeID" in paper_info_df.columns
            and grade_id
        ):
            pr = paper_info_df[paper_info_df["GradeID"].astype(str).str.strip() == grade_id]
            paper_row = pr.iloc[0] if len(pr) else None

        # Lookup machine info
        machine_row = None
        if machine_info_df is not None and "EquipType" in machine_info_df.columns:
            mr = machine_info_df[machine_info_df["EquipType"].astype(str).str.strip() == equip_type]
            machine_row = mr.iloc[0] if len(mr) else None

        if paper_row is None or machine_row is None:
            return pd.Series(
                {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None}
            )

        # Inputs
        basis_wt = float(row.get("BasisWt", 0.0) or 0.0)
        basis_uom = row.get("BasisWtUOM", "LB")
        area_in = float(paper_row.get("Area(IN)", 0.0) or 0.0)
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
        roll_change_hrs = float(machine_row.get("Roll_Change_Hrs", 0.25) or 0.25)
        splits = int(row.get("Splits", 1) or 1)

        # Basis weight to LB if needed
        if basis_uom == "GSM":
            gsm_factor = float(paper_row.get("GSM_Factor", 3100.0) or 3100.0)
            basis_lb = basis_wt / gsm_factor if gsm_factor else basis_wt
        else:
            basis_lb = basis_wt

        if not area_in or not avg_speed or requested_width is None:
            return pd.Series(
                {"LbsPerHour": None, "ConvHrs": None, "ConvertingCostPerCWT": None}
            )

        # Lbs/Hour = BasisWt/(Area*500) * (CutWidth * NumCuts) * (AvgSpeed * 12) * 60
        lbs_per_hour = (
            (basis_lb / (area_in * 500.0))
            * (float(requested_width) * splits)
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
        total_hours = processing_hours + roll_change_hours

        total_cost = total_hours * hourly_rate

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
# LOAD DATA
# =========================================================
df, last_refresh = load_inventory_data()
paper_info_df, machine_info_df = load_supplementary_data()
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
        st.session_state.sel_alt_idx = set()
        st.session_state.search_params = {}
        st.rerun()
    if last_refresh:
        st.info(f"Updated: {last_refresh.strftime('%I:%M %p')}")
    st.metric("Total Items", f"{len(df):,}")
    st.success("✅ Paper Info Loaded" if paper_info_df is not None else "⚠️ Paper Info Missing")
    st.success("✅ Machine Info Loaded" if machine_info_df is not None else "⚠️ Machine Info Missing")

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
            ["All"] + sorted(df["GradeName"].dropna().unique().tolist())
            if "GradeName" in df.columns
            else ["All"]
        )
        grade_name = st.selectbox("Grade Name", gn_opts)

        bw_opts = (
            ["All"] + sorted(df["BasisWt"].dropna().unique().tolist())
            if "BasisWt" in df.columns
            else ["All"]
        )
        basis_wt = st.selectbox("Basis Weight", bw_opts)

        if "Caliper" in df.columns:
            caliper_values = pd.to_numeric(df["Caliper"], errors="coerce").dropna().unique()
            cal_opts = ["All"] + [f"{x:.3f}" for x in sorted(caliper_values)]
            caliper = st.selectbox("Caliper", cal_opts)
        else:
            caliper = "All"

    with col2:
        sheet_width_input = st.text_input("Sheet Width Needed", placeholder='e.g., 48 or 48.5')
        sheet_length_input = st.text_input("Sheet Length Needed", placeholder='e.g., 36 or 36.5')
        roll_width_input = st.text_input("Roll Width Needed (for rolls)", placeholder='e.g., 48 or 48.5')
        max_waste_pct = st.number_input(
            "Max Waste % (for alternatives)",
            min_value=0.0,
            max_value=100.0,
            value=10.0,
            step=1.0,
        )
        max_diameter = st.number_input(
            "Max Diameter (optional)",
            min_value=0.0,
            max_value=60.0,
            value=0.0,
            step=1.0,
            help="Leave at 0 to ignore, or enter max diameter to filter",
        )
        if max_diameter == 0:
            max_diameter = None

    c1, c2 = st.columns([1, 3])
    with c1:
        search_btn = st.form_submit_button("🔍 Search", use_container_width=True)
    with c2:
        reset_btn = st.form_submit_button("🔄 Reset", use_container_width=True)

if reset_btn:
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_idx = set()
    st.session_state.search_params = {}
    st.rerun()


# =========================================================
# SEARCH FUNCTION (Unified)
# =========================================================
def run_search(params):
    warehouse_group = params.get("warehouse_group")
    product_group = params.get("product_group")
    grade_name = params.get("grade_name")
    basis_wt = params.get("basis_wt")
    caliper = params.get("caliper")
    sheet_width_input = params.get("sheet_width_input")
    sheet_length_input = params.get("sheet_length_input")
    roll_width_input = params.get("roll_width_input")
    max_waste_pct = params.get("max_waste_pct")
    max_diameter = params.get("max_diameter")

    filtered = df.copy()

    # Filters
    if "WarehouseGroup" in filtered.columns and warehouse_group != "All":
        filtered = filtered[filtered["WarehouseGroup"] == warehouse_group]

    if product_group != "All":
        if "ProductGroupID" in filtered.columns:
            filtered = filtered[filtered["ProductGroupID"] == product_group]
        elif "ProductGroup" in filtered.columns:
            filtered = filtered[filtered["ProductGroup"] == product_group]

    if "GradeName" in filtered.columns and grade_name != "All":
        filtered = filtered[filtered["GradeName"] == grade_name]

    if "BasisWt" in filtered.columns and basis_wt != "All":
        filtered = filtered[filtered["BasisWt"] == basis_wt]

    if "Caliper" in filtered.columns and caliper != "All":
        filtered = filtered[
            pd.to_numeric(filtered["Caliper"], errors="coerce").round(3) == float(caliper)
        ]

    if max_diameter is not None and "Diameter" in filtered.columns:
        filtered = filtered[filtered["Diameter"] <= max_diameter]

    exact_matches = pd.DataFrame()
    alternative_rolls = pd.DataFrame()
    requested_width = None
    requested_length = None

    # Parse sheet dimensions if provided
    sheet_width = None
    sheet_length = None
    if sheet_width_input:
        try:
            sheet_width = float(str(sheet_width_input).strip())
        except ValueError:
            st.error("❌ Please enter a valid number for Sheet Width")
            return exact_matches, alternative_rolls, None
    
    if sheet_length_input:
        try:
            sheet_length = float(str(sheet_length_input).strip())
        except ValueError:
            st.error("❌ Please enter a valid number for Sheet Length")
            return exact_matches, alternative_rolls, None

    # Parse roll width if provided
    roll_width = None
    if roll_width_input:
        try:
            roll_width = float(str(roll_width_input).strip())
        except ValueError:
            st.error("❌ Please enter a valid number for Roll Width")
            return exact_matches, alternative_rolls, None

    # Inventory value column
    inv_col = None
    for c in ["InvValue", "InvVal", "InventoryValue", "Value"]:
        if c in filtered.columns:
            inv_col = c
            break

    # =========================================================
    # SHEET SEARCH (if sheet width AND length are provided)
    # =========================================================
    if sheet_width is not None and sheet_length is not None:
        requested_width = sheet_width
        requested_length = sheet_length
        
        # Check if sheet dimensions exist in the data
        has_sheet_width = "Sheet_Width" in filtered.columns or "SheetWidth" in filtered.columns
        has_sheet_length = "Sheet_Length" in filtered.columns or "SheetLength" in filtered.columns
        
        if not has_sheet_width or not has_sheet_length:
            st.warning("⚠️ Sheet dimension columns not found in inventory data. Searching for rolls instead based on sheet width.")
            # Fall through to roll search using sheet_width as roll_width
            roll_width = sheet_width
        else:
            # Determine actual column names
            width_col = "Sheet_Width" if "Sheet_Width" in filtered.columns else "SheetWidth"
            length_col = "Sheet_Length" if "Sheet_Length" in filtered.columns else "SheetLength"
            
            filtered = filtered.copy()
            filtered[width_col] = pd.to_numeric(filtered[width_col], errors="coerce")
            filtered[length_col] = pd.to_numeric(filtered[length_col], errors="coerce")
            filtered = filtered.dropna(subset=[width_col, length_col])

            # EXACT MATCHES: Both width AND length must match exactly
            exact_raw = filtered[
                (filtered[width_col].round(2) == round(sheet_width, 2)) &
                (filtered[length_col].round(2) == round(sheet_length, 2))
            ].copy()

            # ALTERNATIVES: Width >= requested AND Length >= requested
            alt_raw = filtered[
                (filtered[width_col] >= sheet_width) &
                (filtered[length_col] >= sheet_length) &
                ~((filtered[width_col].round(2) == round(sheet_width, 2)) &
                  (filtered[length_col].round(2) == round(sheet_length, 2)))
            ].copy()

            # For alternatives, calculate waste based on area
            if len(alt_raw) > 0:
                alt_raw["Requested_Area"] = sheet_width * sheet_length
                alt_raw["Actual_Area"] = alt_raw[width_col] * alt_raw[length_col]
                alt_raw["Waste_Area"] = alt_raw["Actual_Area"] - alt_raw["Requested_Area"]
                alt_raw["Waste_Pct"] = (alt_raw["Waste_Area"] / alt_raw["Actual_Area"]) * 100.0
                alt_raw["Splits"] = 1  # Sheets are typically 1:1
                
                # Filter by max waste percentage
                alt_raw = alt_raw[alt_raw["Waste_Pct"] <= max_waste_pct].copy()

            group_cols = ["GradeName", "BasisWt", "Caliper", width_col, length_col, "Mill", "Brand"]

    # =========================================================
    # ROLL SEARCH (if roll width is provided or fallback from sheet)
    # =========================================================
    elif roll_width is not None and "Roll_Width" in filtered.columns:
        requested_width = roll_width
        
        filtered = filtered.copy()
        filtered["Roll_Width"] = pd.to_numeric(filtered["Roll_Width"], errors="coerce")
        filtered = filtered.dropna(subset=["Roll_Width"])

        # EXACT MATCHES: Roll width must match exactly
        exact_raw = filtered[filtered["Roll_Width"].round(2) == round(roll_width, 2)].copy()

        # ALTERNATIVES: Roll width >= requested (can be slit down)
        larger = filtered[filtered["Roll_Width"] >= roll_width].copy()
        if len(larger) > 0:
            larger["Splits"] = (larger["Roll_Width"] / roll_width).astype(int)
            larger["Waste_Inches"] = larger["Roll_Width"] - (larger["Splits"] * roll_width)
            larger["Waste_Pct"] = (larger["Waste_Inches"] / larger["Roll_Width"]) * 100.0
            alt_raw = larger[larger["Waste_Pct"] <= max_waste_pct].copy()
        else:
            alt_raw = pd.DataFrame()

        group_cols = ["GradeName", "BasisWt", "Caliper", "Roll_Width", "Mill", "Brand"]

    else:
        # No width/dimensions specified
        return exact_matches, alternative_rolls, requested_width

        # -------- Exact grouped --------
        if not exact_raw.empty:
            ex = exact_raw.copy()
            if "Caliper" in ex.columns:
                ex["Caliper"] = pd.to_numeric(ex["Caliper"], errors="coerce")
            if "Roll_Width" in ex.columns:
                ex["Roll_Width"] = pd.to_numeric(ex["Roll_Width"], errors="coerce")

            group_cols_exact = [c for c in group_cols if c in ex.columns]
            agg_dict_ex = {"QtyOnHand": "sum"}
            if inv_col and inv_col in ex.columns:
                agg_dict_ex[inv_col] = "sum"

            exact_matches = ex.groupby(group_cols_exact, as_index=False).agg(agg_dict_ex)

            # AvgCost = (sum inv) / (sum qty) * 100
            if inv_col and inv_col in exact_matches.columns:
                with np.errstate(divide="ignore", invalid="ignore"):
                    exact_matches["AvgCost"] = (
                        exact_matches[inv_col] / exact_matches["QtyOnHand"]
                    ) * 100.0
                    exact_matches["AvgCost"] = exact_matches["AvgCost"].replace(
                        [np.inf, -np.inf], np.nan
                    ).fillna(0.0)

            # Drop inventory value column from display
            if inv_col and inv_col in exact_matches.columns:
                exact_matches = exact_matches.drop(columns=[inv_col], errors="ignore")

        # -------- Alternatives grouped --------
        if not alt_raw.empty:
            al = alt_raw.copy()
            if "Caliper" in al.columns:
                al["Caliper"] = pd.to_numeric(al["Caliper"], errors="coerce")
            if "Roll_Width" in al.columns:
                al["Roll_Width"] = pd.to_numeric(al["Roll_Width"], errors="coerce")

            # Yield = QtyOnHand * (1 - Waste_Pct/100)
            if "QtyOnHand" in al.columns and "Waste_Pct" in al.columns:
                al["Yield"] = al["QtyOnHand"] * (1 - al["Waste_Pct"] / 100.0)

            group_cols_alt = [c for c in group_cols if c in al.columns]
            agg_alt = {
                "QtyOnHand": "sum",
                "Yield": "sum",
                "Splits": "first",
                "Waste_Pct": "first",
            }

            if "Units" in al.columns:
                agg_alt["Units"] = "sum"
            if inv_col and inv_col in al.columns:
                agg_alt[inv_col] = "sum"
            if "GradeID" in al.columns:
                agg_alt["GradeID"] = "first"
            if "BasisWtUOM" in al.columns:
                agg_alt["BasisWtUOM"] = "first"

            alternative_rolls = al.groupby(group_cols_alt, as_index=False).agg(agg_alt)

            # AvgCost (material)
            if inv_col and inv_col in alternative_rolls.columns:
                with np.errstate(divide="ignore", invalid="ignore"):
                    alternative_rolls["AvgCost"] = (
                        alternative_rolls[inv_col] / alternative_rolls["QtyOnHand"]
                    ) * 100.0
                    alternative_rolls["AvgCost"] = alternative_rolls["AvgCost"].replace(
                        [np.inf, -np.inf], np.nan
                    ).fillna(0.0)

            # NetAvgCost = AvgCost × (1 + Waste%)
            if "AvgCost" in alternative_rolls.columns and "Waste_Pct" in alternative_rolls.columns:
                alternative_rolls["NetAvgCost"] = (
                    alternative_rolls["AvgCost"] * (1 + alternative_rolls["Waste_Pct"] / 100.0)
                )

            # Conversion metrics (safe: no .tolist())
            if (
                not alternative_rolls.empty
                and paper_info_df is not None
                and machine_info_df is not None
            ):
                conv_series = alternative_rolls.apply(
                    lambda r: calculate_conversion_cost(
                        r, requested_width, paper_info_df, machine_info_df
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

    return exact_matches, alternative_rolls, requested_width


# =========================================================
# EXECUTE SEARCH (Unified)
# =========================================================
if search_btn:
    # new search → persist params and clear selections
    st.session_state.search_params = {
        "warehouse_group": warehouse_group,
        "product_group": product_group,
        "grade_name": grade_name,
        "basis_wt": basis_wt,
        "caliper": caliper,
        "sheet_width_input": sheet_width_input,
        "sheet_length_input": sheet_length_input,
        "roll_width_input": roll_width_input,
        "max_waste_pct": max_waste_pct,
        "max_diameter": max_diameter,
    }
    st.session_state.sel_exact_idx = set()
    st.session_state.sel_alt_idx = set()

exact_matches = pd.DataFrame()
alternative_rolls = pd.DataFrame()
requested_width = None

if st.session_state.search_params:
    exact_matches, alternative_rolls, requested_width = run_search(st.session_state.search_params)
else:
    st.info("Use the search form above to run a search.")
    st.stop()


# =========================================================
# DISPLAY: EXACT MATCHES
# =========================================================
st.subheader("🎯 Exact Matches")
if not exact_matches.empty:
    header_cols = st.columns([0.5, 1.6, 0.9, 0.9, 1.0, 1.0, 1.0, 1.1, 1.1])
    with header_cols[0]:
        st.markdown("**☑**")
    with header_cols[1]:
        st.markdown("**Grade**")
    with header_cols[2]:
        st.markdown("**BasisWt**")
    with header_cols[3]:
        st.markdown("**Caliper**")
    with header_cols[4]:
        st.markdown("**Width**")
    with header_cols[5]:
        st.markdown("**Mill**")
    with header_cols[6]:
        st.markdown("**Brand**")
    with header_cols[7]:
        st.markdown("**QtyOnHand**")
    with header_cols[8]:
        st.markdown("**AvgCost**")
    st.markdown("---")

    for idx, row in exact_matches.iterrows():
        cols = st.columns([0.5, 1.6, 0.9, 0.9, 1.0, 1.0, 1.0, 1.1, 1.1])
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
            st.write(f"{v:.3f}" if v is not None else "")
        with cols[4]:
            v = row.get("Roll_Width", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else "")
        with cols[5]:
            st.write(row.get("Mill", ""))
        with cols[6]:
            st.write(row.get("Brand", ""))
        with cols[7]:
            v = row.get("QtyOnHand", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else "")
        with cols[8]:
            v = row.get("AvgCost", None)
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else "")

    with st.expander("📋 Exact Details"):
        st.dataframe(exact_matches, use_container_width=True)
else:
    params = st.session_state.search_params
    sw = params.get("sheet_width_input")
    sl = params.get("sheet_length_input")
    rw = params.get("roll_width_input")
    
    if sw and sl:
        st.info(f"No exact matches for sheet dimensions {sw}\" × {sl}\"")
    elif rw:
        st.info(f"No exact matches for roll width {rw}\"")
    elif sw:
        st.info(f"No exact matches for sheet width {sw}\"")
    else:
        st.info("No exact matches found")


# =========================================================
# DISPLAY: ALTERNATIVES
# =========================================================
st.subheader("✂️ Alternatives (Larger Rolls to Split)")
if not alternative_rolls.empty:
    # Ensure required computed columns exist
    for c in ["Yield", "AvgCost", "NetAvgCost", "LbsPerHour", "ConvHrs", "ConvertingCostPerCWT", "FinalCostCWT"]:
        if c not in alternative_rolls.columns:
            alternative_rolls[c] = None

    # 16 fixed columns for Alternatives
    ratios = [
        0.5,  # 0 checkbox
        1.6,  # 1 Grade
        0.9,  # 2 BasisWt
        0.9,  # 3 Caliper
        1.0,  # 4 Width
        1.0,  # 5 Mill
        1.0,  # 6 Brand
        1.1,  # 7 Qty
        0.8,  # 8 Splits
        0.9,  # 9 Waste%
        1.1,  # 10 Yield
        1.1,  # 11 NetAvgCost
        1.1,  # 12 Lbs/Hr
        1.1,  # 13 ConvHrs
        1.1,  # 14 Conv$/CWT
        1.2   # 15 FinalCost/CWT
    ]

    # enforce exact length
    if len(ratios) != 16:
        raise ValueError(f"Expected 16 ratios, got {len(ratios)}")

    H = st.columns(ratios)

    headers = [
        "☑", "Grade", "BasisWt", "Caliper", "Width", "Mill", "Brand",
        "Qty", "Splits", "Waste%", "Yield", "NetAvgCost",
        "Lbs/Hr", "ConvHrs", "Conv$/CWT", "FinalCost/CWT"
    ]

    for i, title in enumerate(headers):
        with H[i]:
            st.markdown(f"**{title}**")

    st.markdown("---")


    for idx, row in alternative_rolls.iterrows():
        C = st.columns(ratios)
        key = f"alt_{idx}"

        with C[0]:
            checked = st.checkbox(
                "sel",
                key=key,
                label_visibility="collapsed",
                value=(idx in st.session_state.sel_alt_idx),
            )
        if checked:
            st.session_state.sel_alt_idx.add(idx)
        else:
            st.session_state.sel_alt_idx.discard(idx)

        with C[1]:  st.write(row.get('GradeName', ''))
        with C[2]:
            v = row.get('BasisWt')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.0f}" if v is not None else '')
        with C[3]:
            v = row.get('Caliper')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.3f}" if v is not None else '')
        with C[4]:
            v = row.get('Roll_Width')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.2f}\"" if v is not None else '')
        with C[5]:  st.write(row.get('Mill', ''))
        with C[6]:  st.write(row.get('Brand', ''))
        with C[7]:
            v = row.get('QtyOnHand')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[8]:
            v = row.get('Splits')
            st.write(f"{int(v)}x" if pd.notna(v) else '')
        with C[9]:
            v = row.get('Waste_Pct')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.1f}%" if v is not None else '')
        with C[10]:
            v = row.get('Yield')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[11]:
            v = row.get('NetAvgCost')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[12]:
            v = row.get('LbsPerHour')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:,.0f}" if v is not None else '')
        with C[13]:
            v = row.get('ConvHrs')
            v = float(v) if pd.notna(v) else None
            st.write(f"{v:.1f}h" if v is not None else '')
        with C[14]:
            v = row.get('ConvertingCostPerCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')
        with C[15]:
            v = row.get('FinalCostCWT')
            v = float(v) if pd.notna(v) else None
            st.write(f"${v:.2f}" if v is not None else '')

    with st.expander("📋 Alternative Details"):
        st.dataframe(alternative_rolls, use_container_width=True)
else:
    params = st.session_state.search_params
    mw = params.get("max_waste_pct", 10.0)
    sw = params.get("sheet_width_input")
    sl = params.get("sheet_length_input")
    rw = params.get("roll_width_input")
    
    if sw and sl:
        st.info(f"No alternatives within {mw}% waste for sheet dimensions {sw}\" × {sl}\"")
    elif rw:
        st.info(f"No alternatives within {mw}% waste for roll width {rw}\"")
    elif sw:
        st.info(f"No alternatives within {mw}% waste for sheet width {sw}\"")
    else:
        st.info(f"No alternatives within {mw}% waste")


# =========================================================
# SUMMARY OF SELECTED + CSV EXPORT
# =========================================================
st.markdown("---")
st.subheader("📊 Summary of Selected")

exact_sel_idx_sorted = sorted(list(st.session_state.sel_exact_idx))
alt_sel_idx_sorted = sorted(list(st.session_state.sel_alt_idx))

selected_exact = (
    exact_matches.iloc[exact_sel_idx_sorted]
    if (not exact_matches.empty and exact_sel_idx_sorted)
    else pd.DataFrame()
)
selected_alt = (
    alternative_rolls.iloc[alt_sel_idx_sorted]
    if (not alternative_rolls.empty and alt_sel_idx_sorted)
    else pd.DataFrame()
)

# Totals
if not selected_exact.empty and "QtyOnHand" in selected_exact.columns:
    total_exact_lbs = selected_exact["QtyOnHand"].fillna(0.0).sum()
else:
    total_exact_lbs = 0.0

if not selected_alt.empty and "Yield" in selected_alt.columns:
    total_alt_yield = selected_alt["Yield"].fillna(0.0).sum()
else:
    total_alt_yield = 0.0

total_lbs = total_exact_lbs + total_alt_yield

# Dollar values
if not selected_exact.empty and set(["AvgCost", "QtyOnHand"]).issubset(selected_exact.columns):
    exact_value = (
        (selected_exact["AvgCost"].fillna(0.0) / 100.0)
        * selected_exact["QtyOnHand"].fillna(0.0)
    ).sum()
else:
    exact_value = 0.0

if not selected_alt.empty and set(["FinalCostCWT", "Yield"]).issubset(selected_alt.columns):
    alt_value = (
        (selected_alt["FinalCostCWT"].fillna(0.0) / 100.0)
        * selected_alt["Yield"].fillna(0.0)
    ).sum()
else:
    alt_value = 0.0

total_value = exact_value + alt_value

if total_lbs > 0:
    blended_cost_cwt = (total_value / total_lbs) * 100.0
else:
    blended_cost_cwt = 0.0

# Summary metrics row (always visible – Option A)
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("Exact Qty Selected", f"{total_exact_lbs:,.0f} lbs")
with c2:
    st.metric("Alt Yield Selected", f"{total_alt_yield:,.0f} lbs")
with c3:
    st.metric("Total Usable Weight", f"{total_lbs:,.0f} lbs")
with c4:
    st.metric("Blended Cost", f"${blended_cost_cwt:,.2f} / CWT")

if not exact_sel_idx_sorted and not alt_sel_idx_sorted:
    st.info("No rows selected yet above.")

# CSV Export of selected rows
export_df = pd.concat([selected_exact, selected_alt], ignore_index=True) if (
    not selected_exact.empty or not selected_alt.empty
) else pd.DataFrame()

if not export_df.empty:
    bw_token = str(basis_wt) if basis_wt != "All" else ""
    
    # Build dimension token based on what was searched
    dim_token = ""
    if sheet_width_input and sheet_length_input:
        dim_token = f"{sheet_width_input}x{sheet_length_input}"
    elif sheet_width_input:
        dim_token = f"SW{sheet_width_input}"
    elif roll_width_input:
        dim_token = f"RW{roll_width_input}"
    
    fname = "NKQuote.csv"
    if bw_token and dim_token:
        fname = f"NKQuote_{bw_token}_{dim_token}.csv"
    elif bw_token:
        fname = f"NKQuote_{bw_token}.csv"
    elif dim_token:
        fname = f"NKQuote_{dim_token}.csv"

    st.download_button(
        "💾 Export Selected to CSV",
        export_df.to_csv(index=False).encode("utf-8"),
        file_name=fname,
        mime="text/csv",
    )
else:
    st.caption("Select rows above to enable CSV export.")
