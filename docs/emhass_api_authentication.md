# EMHASS API token

EMHASS 0.18.10 and later require an API token. EMHASS Companion accepts it during
setup or through the integration's **Reconfigure** form. Keep the API address on
port **5000**; port 8099 belongs to Home Assistant's Supervisor ingress.

For the EMHASS add-on, open EMHASS in the Home Assistant sidebar and select the
key icon (**API token**). Alternatively, set `emhass_api_token` in the add-on's
configuration and restart the add-on. Paste only the token into Companion,
without the `Bearer` prefix.

Older EMHASS installations can leave the field empty. During reconfiguration,
leaving the token field untouched preserves the existing token for the same
address. Enter the token again if you change the address. Use **Remove saved
token** to clear it for a server that does not require authentication.

## Test the authentication repair

1. Install the updated Companion integration and restart Home Assistant.
2. Upgrade the EMHASS add-on to 0.18.11 if it is still on 0.18.9 or earlier.
3. In **Settings → Devices & services**, reload **EMHASS Companion**. If it has
   no valid token, Home Assistant should show an authentication issue in
   **Settings → Repairs**.
4. Open that repair. It should take you directly to the **API token** form.
5. Submit an incorrect token. The form should report that the token is missing
   or incorrect and stay open.
6. Get the correct token from EMHASS and submit it. Companion should reload and
   the repair should disappear, with all other integration settings preserved.
7. Run an optimisation from Companion and check that a new plan arrives.

To test a token change after Companion is already running, change the token in
EMHASS and restart the add-on. The next Companion API request should create the
same repair. An authentication rejection should not replace the existing plan
or appear as a failed optimisation.

If EMHASS reports that authentication is not configured correctly, fix its token
configuration and restart the add-on first. This server configuration error is
different from an incorrect token in Companion.
