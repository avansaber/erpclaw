# Changelog

## Preview 3

- Payments from customers can now be recorded. A payment goes to the open invoice you pick, and if it isn't clear which one, ERPClaw asks. (Paying a supplier's bill is not available yet.) Reversing a payment and cancelling an invoice are available too. Every one of these is saved only after you approve its card.
- An invoice can use the price you state for an item ("5 copper wire at 30 each"). A price more than 10 times the item's list price, or above 1,000,000.00 per unit, is refused, and a percentage is never read as a price. A later price marked as a price ("actually 35 each", "actually $35") corrects the card that is waiting for approval; a plain "make it 7" still changes the quantity.
- When a request includes something ERPClaw can't save yet, such as a rate in another currency, a future invoice date or a date written inside a memo, it asks you instead of guessing.
- Clearer replies: a feature that isn't there yet says so plainly, a refused tax request offers one honest next step, a name filter that matches nothing gives the real total, and after an honest refusal a follow-up names what ERPClaw can do.
- Requests are read more carefully: a detail you say you don't have isn't asked about, "one" before an item is its quantity, "invoice ... and submit" is one change, and a correction such as "no, Wayne, not Acme" uses the named customer or asks.
- If an answer is cut short, the card waiting for your approval is kept.
- To update an existing installation, follow docs/UPDATING.md.

## Preview 2.1

- A tax or discount request written in some plural or common phrasings (for example "with taxes included", "plus taxes" or "at half off") could be left out of an invoice draft. Such requests are now refused with a plain explanation, never silently dropped. Books made with preview 2 open unchanged.

## Preview 2

- A tax or discount request could be dropped after the assistant asked a follow-up question (for example which warehouse, which of two similar items or customers, or how many). It is now applied or refused, never dropped.
