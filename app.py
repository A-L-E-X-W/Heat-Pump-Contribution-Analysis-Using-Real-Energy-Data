import os
import logging
import socket
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from typing import Literal
import uvicorn
from contextlib import asynccontextmanager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Utility Functions ===

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# === Load Data ===

DATA_PATH = "./Data/combined_dataset.parquet"

if not os.path.exists(DATA_PATH):
    raise FileNotFoundError(f"Dataset not found at {DATA_PATH}. Please ensure it's generated and saved properly.")

if DATA_PATH.endswith('.csv'):
    df = pd.read_csv(DATA_PATH, parse_dates=['timestamp'])
elif DATA_PATH.endswith('.parquet'):
    df = pd.read_parquet(DATA_PATH)
else:
    raise ValueError("Unsupported file format")

# Recalculate time-based columns (standardizing with lower-case names)
df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
df['hour'] = df['timestamp'].dt.hour
df['date'] = df['timestamp'].dt.date
# Convert to naive datetime before creating period to avoid timezone drop warnings
df['month'] = df['timestamp'].dt.tz_localize(None).dt.to_period("M")

df['time'] = df['timestamp'].dt.time

# Clean the 'Location' and 'Town' columns by stripping extra whitespace
if 'Location' in df.columns:
    df['Location'] = df['Location'].astype(str).str.strip()
if 'Town' in df.columns:
    df['Town'] = df['Town'].astype(str).str.strip()

# === FastAPI App Setup ===

app = FastAPI(title="Heat Pump Contribution API", lifespan=None)

# === Helper: Generate Plot ===

def generate_comparison_plot(data: pd.DataFrame, location: str) -> BytesIO:
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(data['timestamp'], data['total_energy_kWh'],
            label='Total Consumption (kWh)', color='blue', alpha=0.6)
    # Compute Heat Pump Consumption as total_energy_kWh * (heatpump_pct / 100)
    ax.plot(data['timestamp'], data['total_energy_kWh'] * data['heatpump_pct'] / 100,
            label='Heat Pump Consumption (kWh)', color='orange', alpha=0.8)
    ax.set_title(f"Heat Pump vs Total Energy Consumption\nLocation: {location}")
    ax.set_xlabel("Time")
    ax.set_ylabel("Energy (kWh)")
    ax.legend()
    ax.grid(True)

    buf = BytesIO()
    plt.savefig(buf, format='png')
    plt.close(fig)
    buf.seek(0)
    return buf

# === API Endpoints ===

@app.get("/", response_class=HTMLResponse)
async def root():
    logger.info("Root endpoint accessed")
    return """
    <html>
        <head>
            <title>Heat Pump Contribution API Manual</title>
      <style>
          body {font-family: Arial, sans-serif; margin: 20px; line-height: 1.6;}
          h1 {color: #2c3e50;}
          h2 {color: #34495e;}
          code {background-color: #ecf0f1; padding: 2px 4px; border-radius: 4px;}
          pre {background-color: #ecf0f1; padding: 10px; border-radius: 4px; overflow-x: auto;}
      </style>
    </head>
    <body>
      <h1>Welcome to the Heat Pump Contribution API!</h1>
      <p>This API provides analysis of heat pump energy consumption relative to total facility energy usage. It offers the following endpoints:</p>
      <h2>Endpoints</h2>
      <ul>
        <li><strong>GET /</strong>: Returns this instruction manual.</li>
        <li>
          <strong>GET /heatpump/share</strong>: Returns time-resolved heat pump share data.
          <ul>
            <li><code>location</code> (required): The location name or ID.</li>
            <li><code>resolution</code> (optional): Aggregation level; options: hourly, daily, monthly (default: daily).</li>
          </ul>
        </li>
        <li>
          <strong>GET /heatpump/summary</strong>: Returns summary metrics (average, max, min heat pump percentages) for a given location.
          <ul>
            <li><code>location</code> (required): The location name or ID.</li>
          </ul>
        </li>
        <li>
          <strong>GET /heatpump/plot</strong>: Returns a PNG plot comparing total energy consumption vs. heat pump consumption.
          <ul>
            <li><code>location</code> (required): The location name or ID.</li>
          </ul>
        </li>
        <li>
          <strong>GET /heatpump/report</strong>: Downloads a CSV report with detailed data for a given location.
          <ul>
            <li><code>location</code> (required): The location name or ID.</li>
          </ul>
        </li>
        <li>
          <strong>GET /locations</strong>: Returns a list of all unique locations and their respective counties.
          <ul>
            <li><code>Town</code> (optional): Filter results by Town name.</li>
          </ul>
        </li>
      </ul>
      <h2>How to Use</h2>
      <p>Examples:</p>
      <pre>GET /heatpump/share?location=tregattu%2011&amp;resolution=daily</pre>
      <pre>GET /heatpump/report?location=tregattu%2011</pre>
      <pre>GET /locations?Town=SomeTown</pre>
      <p>Interactive API documentation is available at: <code>/docs</code></p>
    </body>
    </html>
    """

@app.get("/health")
def health_check():
    logger.info("Health check endpoint accessed")
    return {"status": "ok"}

@app.get("/locations")
async def list_locations(Town: str = Query(None, description="Optional Town filter to narrow the list")):
    if Town:
        filtered_df = df[df['Town'].str.contains(Town, case=False, na=False)]
    else:
        filtered_df = df
    # Return a list of dictionaries: each dictionary contains Location and its associated Town.
    locs = filtered_df[['Location', 'Town']].drop_duplicates().sort_values('Location')
    locations_list = locs.to_dict(orient="records")
    return {"locations": locations_list}

@app.get("/heatpump/share")
async def get_heatpump_share(
    location: str = Query(..., description="Location name or ID"),
    resolution: Literal["hourly", "daily", "monthly"] = Query("daily")
):
    location_df = df[df['Location'] == location]

    if location_df.empty:
        raise HTTPException(status_code=404, detail="Location not found")

    if resolution == "hourly":
        # Group by 'date' and 'hour'
        res = location_df.groupby(['date', 'hour'])['heatpump_pct'].mean().reset_index()
    elif resolution == "daily":
        # Group by 'date'
        res = location_df.groupby(['date'])['heatpump_pct'].mean().reset_index()
    else:  # monthly
        # Group by 'month'
        res = location_df.groupby(['month'])['heatpump_pct'].mean().reset_index()

    return res.to_dict(orient="records")

@app.get("/heatpump/summary")
async def get_summary_metrics(
    location: str = Query(..., description="Location name or ID")
):
    location_df = df[df['Location'] == location]
    if location_df.empty:
        raise HTTPException(status_code=404, detail="Location not found")
    
    avg_pct = location_df['heatpump_pct'].mean()
    max_pct = location_df['heatpump_pct'].max()
    min_pct = location_df['heatpump_pct'].min()
    
    return {
        "location": location,
        "average_heatpump_pct": round(avg_pct, 2),
        "max_heatpump_pct": round(max_pct, 2),
        "min_heatpump_pct": round(min_pct, 2),
        "data_points": len(location_df)
    }

@app.get("/heatpump/plot")
async def get_heatpump_plot(
    location: str = Query(..., description="Location name or ID")
):
    location_df = df[df['Location'] == location]
    if location_df.empty:
        raise HTTPException(status_code=404, detail="Location not found")
    
    buf = generate_comparison_plot(location_df, location)
    return StreamingResponse(buf, media_type="image/png")

@app.get("/heatpump/report")
async def download_report(
    location: str = Query(..., description="Location name or ID")
):
    location_df = df[df['Location'] == location]
    if location_df.empty:
        raise HTTPException(status_code=404, detail="Location not found")

    csv_bytes = location_df.to_csv(index=False).encode('utf-8')
    return StreamingResponse(BytesIO(csv_bytes), media_type="text/csv", headers={
        "Content-Disposition": f"attachment; filename={location}_report.csv"
    })

# === Lifespan Context (minimal) ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

# === Main Entrypoint ===

if __name__ == "__main__":
    host = os.getenv("FASTAPI_HOST", get_local_ip())
    port = int(os.getenv("FASTAPI_PORT", get_free_port()))

    # Print detailed instructions for the end user:
    instructions = (
        "\n--------------------------------------------------\n"
        "Heat Pump Contribution API is now running!\n\n"
        "Available Endpoints:\n"
        "1. GET /\n"
        "   - Returns this instruction manual as a formatted HTML page.\n\n"
        "2. GET /heatpump/share\n"
        "   - Parameters:\n"
        "       * location (required): Location name or ID\n"
        "       * resolution (optional): 'hourly', 'daily', or 'monthly' (default: daily)\n"
        "   - Returns time-resolved heat pump share data.\n\n"
        "3. GET /heatpump/summary\n"
        "   - Parameter: location (required)\n"
        "   - Returns summary metrics (average, max, min heat pump contribution percentages) for the location.\n\n"
        "4. GET /heatpump/plot\n"
        "   - Parameter: location (required)\n"
        "   - Returns a PNG plot comparing total consumption vs. heat pump consumption.\n\n"
        "5. GET /heatpump/report\n"
        "   - Parameter: location (required)\n"
        "   - Downloads a CSV report with detailed data for the location.\n\n"
        "6. GET /locations\n"
        "   - Optional Parameter: Town (to filter the list by Town)\n"
        "   - Returns a list of all unique locations along with their associated counties available in the dataset.\n\n"
        "Interactive API documentation is available at: /docs\n"
        "--------------------------------------------------\n"
    )

    print(instructions)
    print(f"Starting FastAPI at http://{host}:{port}/docs")
    uvicorn.run("app:app", host=host, port=port, reload=True)
