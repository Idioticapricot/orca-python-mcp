-- ==============================
-- 1) Update existing agents table
-- ==============================
ALTER TABLE public.agents
ADD COLUMN agent_address TEXT,                -- on-chain agent account (optional but recommended)
ADD COLUMN app_id BIGINT,                     -- agent's smart contract id (optional)
ADD COLUMN price_microalgo BIGINT DEFAULT 0,  -- cost per job / execution price
ADD COLUMN runtime_status TEXT DEFAULT 'inactive' 
    CHECK (runtime_status IN ('active', 'inactive', 'error', 'maintenance'));

-- optional: index for revenue reporting
CREATE INDEX IF NOT EXISTS agents_price_idx ON public.agents(price_microalgo);
CREATE INDEX IF NOT EXISTS agents_runtime_status_idx ON public.agents(runtime_status);


-- ==============================
-- 2) Jobs table (global job lifecycle)
-- ==============================
CREATE TABLE IF NOT EXISTS public.jobs (
  job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id UUID REFERENCES public.agents(id) ON DELETE CASCADE,
  requester_addr TEXT NOT NULL,
  callee_addr TEXT,                           -- used when a2a is added later
  job_input JSONB NOT NULL,
  job_input_hash TEXT NOT NULL,
  amount_microalgo BIGINT,
  state TEXT NOT NULL 
      CHECK (state IN (
        'prepared',
        'payment_pending',
        'onchain_confirmed',
        'running',
        'succeeded',
        'failed',
        'expired',
        'cancelled'
      )),
  txid TEXT,                                   -- representative txid from group
  group_id TEXT,                               -- group hash if needed
  token_id UUID,                               -- FK to tokens.token_id (optional, lazy linked)
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS jobs_agent_id_idx ON public.jobs(agent_id);
CREATE INDEX IF NOT EXISTS jobs_requester_addr_idx ON public.jobs(requester_addr);
CREATE INDEX IF NOT EXISTS jobs_job_input_hash_idx ON public.jobs((job_input_hash));


-- ==============================
-- 3) Tokens (execution authorization audit)
-- ==============================
CREATE TABLE IF NOT EXISTS public.tokens (
  token_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID REFERENCES public.jobs(job_id) ON DELETE CASCADE,
  jwt_jti TEXT UNIQUE,                 -- JWT ID
  jwt_hash TEXT,                       -- hashed token (do NOT store raw token)
  aud TEXT,                            -- agent_id or agent_address
  sub TEXT,                            -- caller wallet address
  exp_at TIMESTAMPTZ,
  revoked BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tokens_job_id_idx ON public.tokens(job_id);


-- ==============================
-- 4) Payments (for explorer + revenue aggregation)
-- ==============================
CREATE TABLE IF NOT EXISTS public.payments (
  payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID REFERENCES public.jobs(job_id) ON DELETE CASCADE,
  agent_id UUID REFERENCES public.agents(id) ON DELETE CASCADE,  -- ✅ allows revenue per agent
  payer_addr TEXT NOT NULL,
  payee_addr TEXT NOT NULL,
  amount_microalgo BIGINT NOT NULL,
  txid TEXT NOT NULL,
  confirmed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS payments_agent_id_idx ON public.payments(agent_id);
CREATE INDEX IF NOT EXISTS payments_job_id_idx ON public.payments(job_id);
CREATE INDEX IF NOT EXISTS payments_payer_addr_idx ON public.payments(payer_addr);
