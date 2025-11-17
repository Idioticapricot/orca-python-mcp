# src/orchestrator/server.py
from mcp.server.fastmcp import FastMCP, Context
from smithery.decorators import smithery
from supabase import create_client, Client
import hashlib
import json
from uuid import uuid4
import os
from dotenv import load_dotenv
from typing import Any, Dict, List, Optional

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
AGENT_DOMAIN = os.getenv("AGENT_DOMAIN", "0rca.live")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def hash_plan(plan: Dict[str, Any]) -> str:
    """Canonical deterministic hash for a plan dict."""
    s = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()





def agent_base_url(subdomain: str) -> str:
    """
    Resolve agent base URL.
    Accept forms:
      - full url with scheme -> returned as-is
      - domain with dot (e.g. pirate.example.com) -> https://{subdomain}
      - short name (e.g. pirateagent) -> https://{subdomain}.{AGENT_DOMAIN}
    """
    if not subdomain:
        raise ValueError("Empty subdomain for agent")
    subdomain = subdomain.strip()
    if subdomain.startswith("http://") or subdomain.startswith("https://"):
        return subdomain.rstrip("/")
    if "." in subdomain:
        return f"https://{subdomain}"
    return f"https://{subdomain}.{AGENT_DOMAIN}"


def fetch_job_with_agent(job_id: str) -> Optional[Dict[str, Any]]:
    """Return job record with joined agent row (agents column) or None."""
    try:
        resp = supabase.table("jobs").select("*, agents(*)").eq("job_id", job_id).single().execute()
        return resp.data
    except Exception:
        return None


@smithery.server()
def create_server():
    server = FastMCP("Orca MCP Orchestrator")

    # ------------------
    # Registry
    # ------------------
    @server.tool()
    def get_registry() -> dict:
        """Return list of agents (raw)."""
        try:
            r = supabase.table("agents").select("*").execute()
            return {"agents": r.data or []}
        except Exception as e:
            return {"error": str(e)}

    # ------------------
    # Format helper
    # ------------------
    @server.tool()
    def create_plan(agent_ids: List[str]) -> dict:
        """Create execution plan from list of agent IDs."""
        try:
            resp = supabase.table("agents").select("*").execute()
            agents = resp.data or []
        except Exception as e:
            return {"error": str(e)}

        plan: List[Dict[str, Any]] = []
        for i, agent_id in enumerate(agent_ids):
            agent = next((a for a in agents if a["id"] == agent_id), None)
            if agent:
                plan.append(
                    {
                        "step": i + 1,
                        "agent_id": agent["id"],
                        "subdomain": agent.get("subdomain"),
                        "price": agent.get("price_microalgo", 0),
                    }
                )
        return {"plan": plan, "estimated_cost": sum(p["price"] for p in plan)}

    # ------------------
    # Planner
    # ------------------
    @server.tool()
    def plan_workflow(intent: str, ctx: Context | None = None) -> dict:
        """
        Smart planner: scores agents by relevance, optionally confirms with LLM.
        Returns plan dict with "plan" (list), "input" (dict), "estimated_cost".
        """
        try:
            resp = supabase.table("agents").select("*").execute()
            agents = resp.data or []
        except Exception as e:
            return {"error": f"error fetching agents: {e}"}

        if not agents:
            return {"plan": [], "estimated_cost": 0, "input": {"prompt": intent}}

        intent_lower = (intent or "").lower()

        def score_agent(agent: Dict[str, Any]) -> int:
            score = 0
            # tags
            tags = agent.get("tags") or []
            if isinstance(tags, list):
                for tag in tags:
                    if tag and isinstance(tag, str) and tag.lower() in intent_lower:
                        score += 4
            # example_input
            ex_in = agent.get("example_input") or ""
            if ex_in and isinstance(ex_in, str):
                for w in ex_in.lower().split():
                    if w in intent_lower:
                        score += 3
                        break
            # description tokens
            desc = (agent.get("description") or "").lower()
            for w in desc.split():
                if w and w in intent_lower:
                    score += 2
            # name tokens
            name = (agent.get("name") or "").lower()
            for w in name.split():
                if w and w in intent_lower:
                    score += 2
            # category
            category = (agent.get("category") or "").lower()
            if category and category in intent_lower:
                score += 1
            return score

        scored = [{"agent": a, "score": score_agent(a)} for a in agents]
        scored.sort(key=lambda x: x["score"], reverse=True)

        best = scored[0]
        chosen = None

        if best["score"] <= 0:
            if len(agents) == 1:
                chosen = best["agent"]
            else:
                confirmed = False
                if ctx and hasattr(ctx, "llm") and callable(getattr(ctx, "llm", None)):
                    prompt = (
                        "You are a concise classifier. Answer 'yes' if the following agent is suitable for the intent, "
                        "otherwise answer 'no'.\n\n"
                        f"Intent: {intent}\n\n"
                        f"Agent name: {best['agent'].get('name')}\n"
                        f"Description: {best['agent'].get('description')}\n"
                        f"Tags: {best['agent'].get('tags')}\n\n"
                        "Answer with yes or no only."
                    )
                    try:
                        llm_resp = ctx.llm(prompt)
                        if isinstance(llm_resp, str) and "yes" in llm_resp.lower():
                            confirmed = True
                    except Exception:
                        pass
                if confirmed:
                    chosen = best["agent"]
        else:
            chosen = best["agent"]

        if not chosen:
            return {"plan": [], "estimated_cost": 0, "input": {"prompt": intent}}

        step = {
            "step": 1,
            "agent_id": chosen["id"],
            "subdomain": chosen.get("subdomain"),
            "price": chosen.get("price_microalgo", 0),
        }

        return {"plan": [step], "input": {"prompt": intent}, "estimated_cost": step["price"]}

    # ------------------
    # Create workflow
    # ------------------
    @server.tool()
    def create_workflow(caller_address: str, plan: Dict[str, Any]) -> dict:
        """
        Persist a workflow and its subjobs. Idempotent via plan hash.
        """
        if not isinstance(plan, dict) or "plan" not in plan:
            return {"error": "plan must be a dict with 'plan' key"}

        plan_list = plan.get("plan", [])
        if not isinstance(plan_list, list) or not plan_list:
            return {"error": "plan['plan'] must be non-empty list"}

        # Validate all steps have required fields
        for step in plan_list:
            if not isinstance(step, dict) or "agent_id" not in step:
                return {"error": "each plan step must have agent_id"}

        p_hash = hash_plan({"caller": caller_address, "plan": plan})

        # Check for existing workflow
        try:
            existing = (
                supabase.table("jobs")
                .select("job_id")
                .eq("job_input_hash", p_hash)
                .eq("requester_addr", caller_address)
                .limit(1)
                .execute()
            )
            if existing.data:
                return {"workflow_id": existing.data[0]["job_id"], "steps": [], "note": "existing_workflow"}
        except Exception as e:
            return {"error": f"failed to check existing workflow: {e}"}

        workflow_id = str(uuid4())
        steps = []

        try:
            # Create parent job
            supabase.table("jobs").insert(
                {
                    "job_id": workflow_id,
                    "requester_addr": caller_address,
                    "job_input": plan,
                    "job_input_hash": p_hash,
                    "state": "prepared",
                }
            ).execute()

            # Create subjobs for each step
            for i, step in enumerate(plan_list):
                subjob_id = str(uuid4())
                supabase.table("jobs").insert(
                    {
                        "job_id": subjob_id,
                        "agent_id": step["agent_id"],
                        "requester_addr": caller_address,
                        "job_input": {"step": i + 1, "agent_id": step["agent_id"]},
                        "job_input_hash": hash_plan({"step": i + 1, "agent_id": step["agent_id"]}),
                        "state": "prepared",
                    }
                ).execute()
                steps.append({"step": i + 1, "subjob_id": subjob_id, "agent_id": step["agent_id"]})

            return {"workflow_id": workflow_id, "steps": steps}
        except Exception as e:
            return {"error": f"failed to create workflow: {e}"}

    # ------------------
    # Get job status
    # ------------------
    @server.tool()
    def get_job_status(job_id: str) -> dict:
        """Fetch job status and details."""
        try:
            job = fetch_job_with_agent(job_id)
            if not job:
                return {"error": "job not found"}
            return {
                "job_id": job["job_id"],
                "state": job["state"],
                "requester_addr": job["requester_addr"],
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------
    # Execute job
    # ------------------
    @server.tool()
    def execute_job(job_id: str) -> dict:
        """Execute a prepared job by calling the agent API."""
        try:
            job = fetch_job_with_agent(job_id)
            if not job or job["state"] != "prepared":
                return {"error": "job not found or not prepared"}
            
            agent = job.get("agents")
            if not agent:
                return {"error": "agent not found"}
            
            # Call agent API
            import httpx
            agent_url = agent_base_url(agent["subdomain"])
            prompt = job["job_input"].get("prompt", "")
            
            response = httpx.post(f"{agent_url}/api/execute", 
                                json={"prompt": prompt}, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                # Update job status
                supabase.table("jobs").update({
                    "state": "completed",
                    "job_output": result
                }).eq("job_id", job_id).execute()
                
                return {"status": "completed", "result": result}
            else:
                return {"error": f"agent call failed: {response.status_code}"}
                
        except Exception as e:
            return {"error": str(e)}

    return server
