# ADR 0005 — HTTPS clone SSRF posture (no IP-pin)

## Status

Accepted (2026-07-22)

## Context

`git clone` re-resolves DNS at fetch time. Validating then cloning by
hostname leaves a residual TOCTOU / DNS-rebinding window. Pinning the
resolved IP in the clone URL breaks TLS hostname verification (cert SAN
mismatch) unless TLS verification is disabled — which we will not do.

Vanilla Kubernetes NetworkPolicy is allow-list only; denying RFC1918 while
still allowing the apiserver (also typically on private IPs) and public
HTTPS is not expressible as a single pod egress policy without an
OVN/Cilium egress firewall or a dedicated clone proxy.

## Decision

1. Allow **HTTPS only** (`_ALLOWED_SCHEMES = {"https"}`).
2. Resolve once at validation; **re-resolve immediately before clone** and
   fail closed if any answer is private / link-local / reserved.
3. Document residual TOCTOU; recommend cluster egress controls (deny
   RFC1918 + `169.254/16` for *git* traffic via a dedicated path, or an
   egress proxy allowlisting known git hosts) — not a blanket portal
   NetworkPolicy deny that would also block kube API.

## Consequences

Application-level checks stop literal private hosts and current private
DNS answers. Residual rebinding between the last resolve and TCP connect
remains an ops concern (egress firewall / proxy), not an in-process IP pin.
