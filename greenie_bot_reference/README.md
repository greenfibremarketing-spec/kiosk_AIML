# Greenie bot (dynamic data)

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=your_key
    uvicorn app:api --reload

    curl -X POST localhost:8000/chat -H "Content-Type: application/json" \
      -d '{"session_id":"u1","message":"Any mug under 500?"}'

Edit data/products.json and the bot reflects it within 30 seconds, with no retraining.
To use another provider, set LLM_MODEL (for example openai:gpt-4o) and install its langchain package.
