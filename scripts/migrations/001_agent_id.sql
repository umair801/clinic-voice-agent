-- Migration 001: tag every row with the agent that produced it. Safe to run more than once.
-- Existing rows become agent_id = 'clinic' (the phone line agent), so nothing already stored changes meaning.
-- Run in the Supabase SQL editor. The application has NOT been changed to depend on this yet.

alter table cva_call_logs    add column if not exists agent_id text not null default 'clinic';
alter table cva_appointments add column if not exists agent_id text not null default 'clinic';
alter table cva_transfers    add column if not exists agent_id text not null default 'clinic';
alter table cva_callbacks    add column if not exists agent_id text not null default 'clinic';

create index if not exists cva_call_logs_agent_ts on cva_call_logs (agent_id, ts);
create index if not exists cva_appointments_agent on cva_appointments (agent_id, created_at);

-- Check afterwards (expect only 'clinic' so far):
-- select agent_id, count(*) from cva_call_logs group by 1;

-- Rollback (only if needed; it drops the tag, not the rows):
-- alter table cva_call_logs drop column agent_id;
-- alter table cva_appointments drop column agent_id;
-- alter table cva_transfers drop column agent_id;
-- alter table cva_callbacks drop column agent_id;
