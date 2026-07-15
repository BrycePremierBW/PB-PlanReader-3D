# Premier Brushworks Plan Reader & 3D Take-off — Standalone v1

This is a **separate Streamlit app**. It does not patch or replace JobHub.

It can connect to the same JobHub database to:

- use the same JobHub usernames and passwords;
- show the live JobHub job register;
- search common document/attachment tables for files linked to the selected job;
- record PlanReader uploads against the JobHub job;
- send a reviewed take-off back to JobHub as a new draft package.

PlanReader keeps its own workspaces, rendered drawing pages, mapped zones and model files on its own persistent storage.

## Main workflow

1. Open a live JobHub job or create a standalone workspace.
2. Import linked JobHub documents and/or upload plans, specifications and schedules.
3. Process PDFs into drawing-page images and text.
4. Review the drawing register and drawing types.
5. Run the optional AI plan read or build the take-off manually.
6. Map calibrated rectangles over floor plans and elevations.
7. Create measured, derived or assumed building masses.
8. Review the interactive 3D model and take-off quantities.
9. Download the Excel/ZIP/OBJ/interactive HTML package.
10. Optionally send the reviewed draft take-off to JobHub.

## Subscription take-off method included

The export pack contains:

- Project Information
- Executive Summary
- Source Documents
- Drawing Register
- Source & Basis
- Take-off Schedule
- Door Schedule
- Inclusions
- Exclusions
- Clarifications
- Assumptions
- RFIs
- Colours & Finishes
- Access Constraints
- Risks
- Mapped Zones
- 3D Masses
- 3D Openings

## Important accuracy limitation

The 3D result is a **take-off and estimating model**, not construction-certified BIM.

- **Measured / Verified** means a human should have confirmed the source dimensions.
- **Derived** means the geometry was calculated from calibrated mapping or drawing relationships.
- **Assumed** means a placeholder was used because the documents were incomplete.

The AI prompt is instructed not to invent dimensions. Anything unsupported should remain `To measure`, `To review`, or `Assumed`. Always check the current issued-for-construction plans and specifications before pricing or construction use.

## Windows setup

1. Extract this folder.
2. Optional: set environment variables from `.env.example`.
3. Double-click `RUN_PLANREADER_WINDOWS.bat`.

For a local link to a SQLite JobHub database, set:

```text
JOBHUB_DB_PATH=C:\full\path\to\your\jobhub.db
```

## Render setup — separate service

Deploy this folder as a **new Render web service**.

Recommended settings:

```text
Build command: pip install -r requirements.txt
Start command: streamlit run pb_planreader_3d_app.py --server.address 0.0.0.0 --server.port $PORT
```

Set:

```text
JOBHUB_DATABASE_URL=<the exact PostgreSQL URL used by JobHub>
OPENAI_API_KEY=<optional, required only for AI plan reading>
OPENAI_MODEL=gpt-5.6
PLANREADER_DATA_DIR=/var/data/planreader
```

Attach a persistent disk at `/var/data`.

## Linked-document storage rule

Two separate Render services cannot read each other's private local disk paths.

PlanReader can import a JobHub-linked file when the JobHub database record contains one of:

- the actual file bytes/blob;
- a public or signed download URL;
- a filesystem path reachable from the PlanReader service.

If JobHub only stores a path on JobHub's own Render disk, upload the file to PlanReader or move shared files to object storage. PlanReader uploads are saved on the PlanReader disk and their metadata can be linked to the JobHub job in the shared database.

## AI configuration

AI plan reading is optional. Manual mapping, take-off editing, Excel export and 3D modelling work without an API key.

The app uses image inputs and structured JSON output through the OpenAI Responses API. The default model can be changed with `OPENAI_MODEL`.

## Files

- `pb_planreader_3d_app.py` — complete app
- `requirements.txt` — Python dependencies
- `render.yaml` — example separate Render service
- `RUN_PLANREADER_WINDOWS.bat` — Windows launcher
- `.env.example` — environment variable guide
- `TEST_REPORT.txt` — completed smoke tests
