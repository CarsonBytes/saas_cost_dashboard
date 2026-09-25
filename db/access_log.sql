-- access_log: tracks every dashboard page load with IP, region, user agent,
-- referer, and timestamp.  Used for the "Access Log" tab in the dashboard.
-- Run this in the Supabase SQL editor.

create table if not exists access_log (
  id         uuid primary key default gen_random_uuid(),
  ts         timestamptz not null default now(),
  path       text not null,
  method     text not null default 'GET',
  ip         text,
  region     text,
  user_agent text,
  referer    text,
  is_bot     boolean default false
);

create index if not exists idx_access_log_ts on access_log(ts desc);
create index if not exists idx_access_log_ip on access_log(ip);

-- Row Level Security: only the service role (backend) can write;
-- authenticated users (the dashboard owner) can read.
alter table access_log enable row level security;

create policy "Service role can insert access logs"
  on access_log for insert
  to service_role
  with check (true);

create policy "Authenticated users can read access logs"
  on access_log for select
  to authenticated
  using (true);
