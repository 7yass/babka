"""Defer-first interaction helpers.

Discord answers a component click only if the interaction is acknowledged
within ~3 seconds; anything slower reads to the user as "the application
did not respond". These helpers make every click handler acknowledge first
and finish late, in whichever way the interaction still allows.
"""


async def ack(ix) -> str:
    """Acknowledge a component/modal interaction, idempotently.

    Returns the completion mode for this interaction:
      'edit'     -- deferred (message update): finish by editing the original
      'followup' -- already acknowledged, or the token is dead: finish via
                    a followup message
    """
    try:
        await ix.response.defer()
        return 'edit'
    except Exception:
        # InteractionResponded = the slot was taken (someone answered already);
        # any other discord HTTP error = dead/expired token. Either way the
        # only remaining completion path is a followup message.
        return 'followup'


async def finish(ix, mode: str, **kw):
    """Complete an acked interaction. Never raises.

    'edit' edits the message the component sits on (view/attachments/content);
    'followup' posts a followup message instead. Falls back to a followup when
    the original edit fails for any reason.
    """
    if mode == 'edit':
        try:
            return await ix.edit_original_response(**kw)
        except Exception:
            pass
    try:
        return await ix.followup.send(**kw)
    except Exception:
        return None
