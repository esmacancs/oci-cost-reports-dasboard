# OCI Cost Dashboard

A Flask-based web dashboard to visualize Oracle Cloud Infrastructure (OCI) cost and usage data with resource management capabilities.

## Features

- Cost breakdown by service, compartment, and resource
- Daily and monthly trend charts
- Cost forecast based on recent usage
- Top cost-generating resources with enriched metadata
- Resource actions: Start, Stop, Delete (admin only)
- CSV export
- Auto-refresh (5-minute polling)
- Login with role-based access control (RBAC)
- Dockerized deployment

---

## Prerequisites

- Python 3.11+ (for local run)
- Docker & Docker Compose (for containerized run)
- OCI API key and config file

---

## Quick Start (Docker)

### 1. Prepare OCI Credentials

Place these two files in the project root (`E:\oci-cost-reports\`):

```
oci_config          # OCI config file
oci_api_key.pem     # OCI private key
```

Example `oci_config`:

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaa...
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx
tenancy=ocid1.tenancy.oc1..aaaaaaaa...
region=me-dcc-muscat-1
key_file=/root/.oci/oci_api_key.pem
```

### 2. Build & Run

```bash
cd E:\oci-cost-reports
docker compose up --build
```

### 3. Access the Dashboard

Open [http://localhost:5000](http://localhost:5000)

### 4. Login

| Role   | Username | Password  | Access                          |
|--------|----------|-----------|---------------------------------|
| Admin  | `admin`  | `admin123`| Full access + resource actions  |
| Viewer | `viewer` | `viewer123`| View only (no Start/Stop/Delete)|

---

## Local Setup (without Docker)

### 1. Install Python

```bash
winget install Python.Python.3.11
```

### 2. Install Dependencies

```bash
cd E:\oci-cost-reports
pip install -r requirements.txt
```

### 3. Run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

---

## Project Structure

```
oci-cost-reports/
├── app.py                 # Flask backend (routes, OCI API, RBAC)
├── templates/
│   ├── index.html         # Dashboard UI
│   └── login.html         # Login page (split-screen)
├── pull_costs.py          # Standalone script for Excel export
├── requirements.txt       # Python dependencies
├── oci_config             # OCI config (git-ignored)
├── oci_api_key.pem        # OCI private key (git-ignored)
├── Dockerfile             # Container image
├── docker-compose.yml     # Docker Compose config
├── entrypoint.sh          # Container entrypoint
├── .gitignore
├── .dockerignore
└── README.md
```

---

## RBAC Permissions

| Action          | Admin | Viewer |
|-----------------|-------|--------|
| View dashboard  | Yes   | Yes    |
| Export CSV      | Yes   | Yes    |
| Start resource  | Yes   | No     |
| Stop resource   | Yes   | No     |
| Delete resource | Yes   | No     |

---

## Supported Resource Actions

| Action     | Compute | PostgreSQL | Network Firewall | Load Balancer | Block Volumes | VCN/Subnet | OKE |
|------------|---------|------------|------------------|---------------|---------------|------------|-----|
| Start/Stop | Yes     | -          | -                | -             | -             | -          | -   |
| Delete     | Yes     | Yes        | Yes              | Yes           | Yes           | Yes        | Yes |

---

## Environment Variables

| Variable         | Default                       | Description                     |
|------------------|-------------------------------|---------------------------------|
| `OCI_CONFIG_PATH`| `./oci_config` (local)        | Path to OCI config file         |
| `FLASK_ENV`      | `production`                  | Flask environment mode          |

---

## Troubleshooting

**Docker: OCI config error**
- Ensure `oci_config` and `oci_api_key.pem` are in the project root
- Verify `key_file` path in config matches the container path (entrypoint handles this automatically)

**Local: Python not found**
```bash
py -3.11 --version
```
If not found, reinstall with `winget install Python.Python.3.11` and restart terminal.

**Port already in use**
```bash
# Change port in docker-compose.yml
ports:
  - "5001:5000"
```
