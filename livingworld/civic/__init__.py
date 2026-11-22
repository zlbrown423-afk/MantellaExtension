"""Civic core (plan C1 + C2, Whiterun only): institutions and a paper coin ledger.

The approved civic plan (RP society systems, 2026-11-08) puts the shared building blocks of government and money
in one package so guards, companies, courts and settlements do not each grow their own. This first slice holds
only what the lordship milestones need:

* ``institutions``: bodies that hold authority or money (the Jarl's court, the Plains Wardens), on the save
  timeline (migration 027).
* ``ledger``: purses and append-only ledger entries. A balance is the sum of its entries; a transfer is two
  entries sharing a ``transfer_key``. Money here is on paper only and never touches the player's inventory.

Everything takes an open connection inside a transaction; the caller owns it.
"""
