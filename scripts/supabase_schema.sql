-- Clinic Voice Agent: backend tables. Synthetic data only. Apply once in the Supabase SQL editor.
-- The idempotency guarantee lives here: UNIQUE(idempotency_key) and one atomic function.

create table if not exists cva_patients (
  id text primary key, first_name text not null, last_name text, dob date not null
);
create index if not exists cva_patients_name_dob on cva_patients (lower(first_name), dob);

create table if not exists cva_slots (
  slot_id text primary key, provider_id text not null, start text not null, taken_by text
);

create table if not exists cva_appointments (
  appointment_id text primary key,
  idempotency_key text not null unique,
  call_id text, patient_id text references cva_patients(id), slot_id text references cva_slots(slot_id),
  provider_id text, start text, status text default 'booked', created_at timestamptz default now()
);
create table if not exists cva_transfers (id bigserial primary key, call_id text, reason text, created_at timestamptz default now());
create table if not exists cva_callbacks (id bigserial primary key, call_id text, note text, created_at timestamptz default now());

alter table cva_patients enable row level security;
alter table cva_slots enable row level security;
alter table cva_appointments enable row level security;
alter table cva_transfers enable row level security;
alter table cva_callbacks enable row level security;
-- No policies on purpose: only the service-role key (server side) can read or write.

create or replace function cva_book(p_call_id text, p_patient_id text, p_slot_id text, p_key text)
returns jsonb language plpgsql as $$
declare
  existing cva_appointments%rowtype;
  s cva_slots%rowtype;
  new_id text;
begin
  select * into existing from cva_appointments where idempotency_key = p_key;
  if found then return jsonb_build_object('outcome','replay','record',to_jsonb(existing)); end if;
  if not exists (select 1 from cva_patients where id = p_patient_id) then
    return jsonb_build_object('outcome','unknown_patient');
  end if;
  select * into s from cva_slots where slot_id = p_slot_id for update;
  if not found then return jsonb_build_object('outcome','unknown_slot'); end if;
  if s.taken_by is not null then
    select * into existing from cva_appointments where slot_id = p_slot_id and patient_id = p_patient_id;
    if found then return jsonb_build_object('outcome','duplicate_prevented','record',to_jsonb(existing)); end if;
    return jsonb_build_object('outcome','slot_unavailable');
  end if;
  new_id := 'apt_' || lpad((select count(*) + 1 from cva_appointments)::text, 5, '0') || '_' || substr(md5(p_key), 1, 4);
  insert into cva_appointments(appointment_id, idempotency_key, call_id, patient_id, slot_id, provider_id, start)
    values (new_id, p_key, p_call_id, p_patient_id, p_slot_id, s.provider_id, s.start);
  update cva_slots set taken_by = new_id where slot_id = p_slot_id;
  select * into existing from cva_appointments where appointment_id = new_id;
  return jsonb_build_object('outcome','created','record',to_jsonb(existing));
exception when unique_violation then
  select * into existing from cva_appointments where idempotency_key = p_key;
  return jsonb_build_object('outcome','replay','record',to_jsonb(existing));
end $$;
