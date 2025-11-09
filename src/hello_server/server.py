from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from smithery.decorators import smithery
from supabase import create_client, Client
import httpx
import hashlib
import json
from uuid import uuid4

SUPABASE_URL = "https://zmbxocwnisqvkyqpqdkh.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InptYnhvY3duaXNxdmt5cXBxZGtoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1NjYyODMsImV4cCI6MjA3NzE0MjI4M30.bzc88z_WwX5XsN10jt1iDgFdKqLid1JIOxGxzsp4IR0"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@smithery.server()
def create_server():
    server = FastMCP("Orca Orchestrator")

    @server.tool()
    def get_registry() -> str:
        """Get the list of available agents from the registry."""
        response = supabase.table("agents").select("*").execute()
        return json.dumps(response.data, indent=2)

    @server.tool()
    def create_job(agent_id: str, caller_address: str, job_input: dict) -> str:
        """Create a new job for an agent. Returns job_id."""
        job_id = str(uuid4())
        job_input_str = json.dumps(job_input, sort_keys=True)
        job_input_hash = hashlib.sha256(job_input_str.encode()).hexdigest()
        
        job_data = {
            "job_id": job_id,
            "agent_id": agent_id,
            "requester_addr": caller_address,
            "job_input": job_input,
            "job_input_hash": job_input_hash,
            "state": "prepared"
        }
        
        supabase.table("jobs").insert(job_data).execute()
        return json.dumps({"job_id": job_id, "status": "created"})

    @server.tool()
    def prepare_job(job_id: str) -> str:
        """Prepare a job by calling agent's /start_job/prepare endpoint. Returns unsigned transaction bundle."""
        job_response = supabase.table("jobs").select("*, agents(subdomain)").eq("job_id", job_id).single().execute()
        job = job_response.data
        
        agent_subdomain = job["agents"]["subdomain"]
        agent_url = f"https://{agent_subdomain}.0rca.live/start_job/prepare"
        
        payload = {
            "job_id": job_id,
            "job_input": job["job_input"]
        }
        
        with httpx.Client() as client:
            response = client.post(agent_url, json=payload, timeout=30.0)
            
            if response.status_code == 402:
                result = response.json()
                supabase.table("jobs").update({"state": "payment_pending"}).eq("job_id", job_id).execute()
                return json.dumps(result, indent=2)
            else:
                return json.dumps({"error": f"Unexpected status {response.status_code}", "body": response.text})

    return server
