## Setup Instructions

### 1. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure MongoDB

Set the following environment variables before starting the application:

```text
MONGO_URI=your_mongodb_connection_string
MONGO_DB=your_database_name
```

Do not commit database credentials or `.env` files to the repository.

### 4. Load sample data (optional)

If the provided sample data loader is available, run:

```bash
python sample_seed.py
```

### 5. Run the application

```bash
uvicorn app.main:app --port 8000
```

Open Swagger UI at http://127.0.0.1:8000/docs to explore and test the API.