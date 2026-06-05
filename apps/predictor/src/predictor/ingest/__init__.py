"""Historical + live data ingestion layer.

Each submodule exposes a typed source ``Protocol`` so the loaders can be
unit-tested against fixtures without touching the network. Production
adapters (FBref, Football-Data.co.uk, the-odds-api, 1xbet) implement the
protocols.
"""
