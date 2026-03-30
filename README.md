# Travel Photography Map

An automated system to create interactive map-based travel photo galleries. Display GPX routes on maps with geotagged photos clustered at capture locations.

## Quick Start

### Prerequisites

1. **Install ExifTool** (required for geotagging):
   ```bash
   brew install exiftool
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

### View the Web Interface

Start the web server:

```bash
cd web
python -m http.server 8000
```

Open http://localhost:8000 in your browser.

**How it works:**
- All processed trips are automatically displayed on the same map
- Each trip gets a different colored route
- Photo markers are color-coded to match their trip's route
- The trips index is automatically updated when you process new trips

### Process Your Own Trip

```bash
python process_trip.py \
  --name "My Trip 2024" \
  --gpx /path/to/route.gpx \
  --photos /path/to/photos \
  --output web/trips/my-trip
```

**Processing multiple trips:**

Simply run the script multiple times with different trips. Each trip will be automatically added to the map:

```bash
# First trip
python process_trip.py \
  --name "Iceland 2024" \
  --gpx iceland.gpx \
  --photos ~/Photos/Iceland \
  --output web/trips/iceland-2024

# Second trip
python process_trip.py \
  --name "Japan 2023" \
  --gpx japan.gpx \
  --photos ~/Photos/Japan \
  --output web/trips/japan-2023
```

All trips will appear together on the same map with different colored routes.

## Usage

### Processing Script

```bash
python process_trip.py [OPTIONS]

Options:
  --name TEXT           Trip name for display (required)
  --gpx PATH            Path to GPX file (required)
  --photos PATH         Path to photos directory (required)
  --output PATH         Output directory (required)
  --geosync TEXT        Timezone offset for camera sync (e.g., +02:00)
  --cluster-radius INT  Clustering radius in meters (default: 50)
  --test-mode INT       Process only X% of photos for faster testing (e.g., 10)
  --dry-run             Preview without writing files
  --help                Show this message and exit
```

### Example

```bash
# Process Iceland trip photos
python process_trip.py \
  --name "Iceland Ring Road 2024" \
  --gpx ~/Downloads/iceland-strava.gpx \
  --photos ~/Lightroom/Iceland-2024 \
  --output web/trips/iceland-2024 \
  --geosync +00:00

# Preview without writing files
python process_trip.py \
  --name "Test" \
  --gpx test.gpx \
  --photos ./photos \
  --output ./output \
  --dry-run

# Test with 10% of photos for faster iteration
python process_trip.py \
  --name "Test" \
  --gpx test.gpx \
  --photos ./photos \
  --output ./output \
  --test-mode 10
```

## Output Structure

After processing, you'll get:

```
web/trips/iceland-2024/
├── thumbnails/       # 300px images (~60KB each)
├── display/          # 1920px images (~350KB each)
├── route.geojson     # Converted GPX for web
└── manifest.json     # Trip metadata
```

## Project Structure

```
geotag-photos-map/
├── process_trip.py       # Main CLI script
├── requirements.txt      # Python dependencies
├── web/                  # Static website
│   ├── index.html
│   ├── css/styles.css
│   ├── js/app.js
│   └── trips/            # Processed trip data
│       └── index.json    # Auto-generated trips index
└── README.md
```

## Features

### Photo Processing
- Recursive photo discovery (JPG, JPEG, PNG, TIFF)
- EXIF timestamp extraction
- GPS interpolation from GPX trackpoints
- Timezone offset support
- Multi-size image generation (thumbnail + display)
- Proximity-based clustering

### Web Interface
- Full-screen Leaflet map
- GPX route overlay
- Clustered photo markers
- GLightbox gallery with swipe navigation
- EXIF info toggle
- Mobile responsive

## Preparing Your Data

### Export GPX from Strava
1. Open your activity on Strava
2. Click the three dots (...)
3. Select "Export GPX"

### Prepare Photos
- Photos need valid EXIF DateTimeOriginal timestamps
- Camera time must be synchronized with GPS time
- Use `--geosync` to adjust for timezone differences

## Technology Stack

- **Python**: Pillow, gpxpy, Click, tqdm
- **Web**: Leaflet.js, MarkerCluster, GLightbox
- **Tools**: ExifTool (geotagging)
