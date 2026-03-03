# How to deploy

```bash
cd slack-agent/

export GOOGLE_CLOUD_PROJECT="your-project-id"
export REGION="us-central1"
export SERVICE_NAME="gcp-slack-assistant"

# Enable APIs
gcloud services enable \
    run.googleapis.com \
    secretmanager.googleapis.com \
    developerknowledge.googleapis.com \
    aiplatform.googleapis.com \
    --project=$GOOGLE_CLOUD_PROJECT

# Grant the default compute SA access to secrets
PROJECT_NUMBER=$(gcloud projects describe $GOOGLE_CLOUD_PROJECT --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"

# Deploy to Cloud Run
gcloud run deploy $SERVICE_NAME \
    --source . \
    --region $REGION \
    --project $GOOGLE_CLOUD_PROJECT \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}" \
    --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true" \
    --set-env-vars "MODEL=gemini-2.5-flash" \
    --set-env-vars "MCP_HTTP_DEBUG=false" \
    --allow-unauthenticated \
    --timeout 120 \
    --memory 1Gi \
    --cpu 1 \
    --min-instances 1 \
    --max-instances 5 \
    --concurrency 20

# Get the URL
URL=$(gcloud run services describe $SERVICE_NAME \
    --region $REGION --format='value(status.url)')
echo "✅ Service URL: ${URL}"
echo "👉 Set Slack Event URL to: ${URL}/slack/events"
```


## Configure Slack

### Option 1: Create via UI

1. Go to https://api.slack.com/apps
2. Click "Create New App" → "From a manifest"
3. Select your workspace
4. Choose JSON format
5. Paste the `manifest.json`
6. Replace `https://YOUR-CLOUD-RUN-URL` with your actual Cloud Run URL
7. Click Create

### Option 2: Create via Slack CLI

```bash
# Install Slack CLI: https://api.slack.com/automation/cli/install

# Replace the URL first
sed -i 's|YOUR-CLOUD-RUN-URL|your-service-abc123-uc.a.run.app|g' manifest.json

# Create the app
slack app create --manifest manifest.json
```

## Store Slack secrets
```bash
echo -n "xoxb-your-bot-token" | gcloud secrets create slack-bot-token \
    --data-file=- --project=$GOOGLE_CLOUD_PROJECT

echo -n "your-signing-secret" | gcloud secrets create slack-signing-secret \
    --data-file=- --project=$GOOGLE_CLOUD_PROJECT
    ```

## Local Development

### Set env vars instead of Secret Manager

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_SIGNING_SECRET="..."
export GOOGLE_CLOUD_PROJECT="your-project"
```

### Run locally

```bash
python main.py
```

### Expose with ngrok for Slack to reach you

```bash
ngrok http 8080
```
*→ Use the ngrok URL as your Slack Event URL*