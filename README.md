# Deploy the interactive lab with Streamlit

## Run locally

Use Python 3.13 (tested) and Git:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open http://localhost:8501. Choose a preset or edit inputs, then click **Run
simulation**. Editing inputs does not launch a simulation. Previous results
stay visible with a warning until the next successful run. Downloads belong
to the displayed run, and session results are not cached across visitors.

## Community Cloud

1. Create a dedicated GitHub repository from the contents of the generated
   `energy-lab-streamlit` folder. Do not upload this full research checkout:
   it contains original project documents and history.
2. Sign in to https://share.streamlit.io and complete account onboarding.
3. Select **Create app**, then **Yup, I have an app**.
4. Choose the new repository, its default branch, and `streamlit_app.py`.
5. In **Advanced settings**, select Python **3.13**. No app secrets or API keys
   are required. The requirements file pins Streamlit and the Nandatown revision.
6. Deploy, wait for startup, and run the normal scenario. It should commit and
   settle 9 kWh; the cloud-shortfall scenario commits 9 and settles 7 kWh.

Official deployment guide:
https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy

The archive contains runtime Python code, synthetic scenarios, dependency
metadata, Streamlit theme, this README, and automated tests. It excludes
`context/`, local Git history, generated traces, signing keys and local secrets.
Create a repository in your own account; hosting and repository access settings
are managed by the account owner. The app contains only synthetic data.

## Execution and limits

Streamlit runs the Python interface and calls the same isolated Nandatown worker
used by the FastAPI demo. There is no separate API service to deploy. Each run
has a 15-second timeout and temporary signing keys/files that are cleaned up.
A thread-safe process-wide gate allows two concurrent runs and 60 accepted runs
per minute. Only capacity counters are shared; inputs and results remain per
session. Each visitor retains their latest result plus the previous result's
summary for comparison. No database or durable run storage is used.

The scientific scope is unchanged: six synthetic households, deterministic
policies, one interval, INR/kWh, configurable missing-meter behavior, aggregate
utility cap, no electrical power flow, no production integration and no LLM.

## Validation

`python -m unittest discover -s tests -q` runs the complete suite in the full
research checkout. The deployment archive includes the Streamlit and HTTP tests
only; run the same command there. AppTest covers unrun edits, budget-dependent
results, presets/reset, missing meters, session isolation, lifecycle navigation,
download controls and error recovery. Backend tests verify native execution,
input validation, cleanup, and concurrency. Remote deployment is complete only
once the hosted URL has been opened and a fresh simulation has succeeded.

## Network replay

Select a scenario in the sidebar and Run simulation. Play or step through the
recorded offers, checks, commitments and settlements. Click a node to inspect
its rules; click a trade to highlight it. Detailed inputs, results and evidence
are expandable. The graph represents logical exchanges, not physical power
lines. The current agents use deterministic policies without price bargaining.
