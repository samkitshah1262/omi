from fastapi import Request, Header, HTTPException, APIRouter, Depends
import stripe

from database.users import get_stripe_connect_account_id, set_stripe_connect_account_id, set_paypal_payment_details, \
    get_default_payment_method, set_default_payment_method, get_paypal_payment_details
from utils import stripe as stripe_utils
from utils.apps import paid_app
from utils.other import endpoints as auth
from fastapi.responses import HTMLResponse

from utils.stripe import create_connect_account, refresh_connect_account_link, \
    is_onboarding_complete

router = APIRouter()


@router.post('/v1/stripe/webhook', tags=['v1', 'stripe', 'webhook'])
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    try:
        event = stripe_utils.parse_event(payload, stripe_signature)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Invalid signature")

    print("stripe_webhook event", event['type'])

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']  # Contains session details
        print(f"Payment completed for session: {session['id']}")

        app_id = session['metadata']['app_id']
        client_reference_id = session['client_reference_id']
        if not client_reference_id or len(client_reference_id) < 4:
            raise HTTPException(status_code=400, detail="Invalid client")
        uid = client_reference_id[4:]

        # paid
        paid_app(app_id, uid)

    if event['type'] == 'account.updated':
        # this event occurs for the connected account, check if the account is fully onboarded to set default method
        account = event['data']['object']
        if account['charges_enabled'] and account['details_submitted']:
            # account is fully onboarded
            uid = account['metadata']['uid']
            if get_default_payment_method(uid) is None:
                set_default_payment_method(uid, 'stripe')

    # TODO: handle this event to link transfers?
    if event['type'] == 'transfer.created':
        transfer = event['data']['object']
        print(transfer)

    return {"status": "success"}


@router.post("/v1/stripe/create-connect-account")
async def create_connect_account_endpoint(request: Request, uid: str = Depends(auth.get_current_user_uid)):
    """
    Create a Stripe Connect account and return the account creation response
    """
    try:
        account_id = get_stripe_connect_account_id(uid)
        base_url = str(request.base_url).rstrip('/')

        if account_id:
            account = refresh_connect_account_link(account_id, base_url)
        else:
            account = create_connect_account(base_url, uid)
            set_stripe_connect_account_id(uid, account['account_id'])

        return account

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/v1/stripe/onboarded", tags=['v1', 'stripe'])
async def check_onboarding_status(uid: str = Depends(auth.get_current_user_uid)):
    """
    Check the onboarding status of a Connect account
    """
    try:
        account_id = get_stripe_connect_account_id(uid)
        if not account_id:
            return {"onboarding_complete": False}
        return {"onboarding_complete": is_onboarding_complete(account_id)}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/v1/stripe/refresh/{account_id}")
async def refresh_account_link_endpoint(request: Request, account_id: str,
                                        uid: str = Depends(auth.get_current_user_uid)):
    """
    Generate a fresh account link if the previous one expired
    """
    try:
        base_url = str(request.base_url).rstrip('/')
        account = refresh_connect_account_link(account_id, base_url)
        return account
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/v1/stripe/return/{account_id}", response_class=HTMLResponse)
async def stripe_return(account_id: str):
    """
    Handle the return flow from Stripe Connect account creation
    """
    onboarding_complete = is_onboarding_complete(account_id)
    title = "Stripe Account Setup Complete" if onboarding_complete else "Stripe Account Setup Incomplete"
    message_class = "" if onboarding_complete else "error"
    message = "Your Stripe account has been successfully set up with Omi AI. You can now start receiving payments." if onboarding_complete \
        else "The account setup process was not completed. Please try again in a few minutes. If the issue persists, contact support."

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Return to App</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{                
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                margin: 0;
                padding: 20px;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                box-sizing: border-box;
            }}
            .heading {{
                font-size: clamp(20px, 5vw, 24px);
                font-weight: bold;
                margin-bottom: 20px;
                color: #333;
                text-align: center;
            }}
            .message {{
                font-size: clamp(14px, 4vw, 16px);
                color: #666;
                text-align: center;
                margin-bottom: 30px;
                max-width: 600px;
                line-height: 1.5;
            }}
            .close-instruction {{
                font-size: clamp(14px, 4vw, 16px);
                color: #4CAF50;
                text-align: center;
                margin-top: 20px;
            }}
            .error {{
                color: #d32f2f;
            }}
        </style>
    </head>
    <body>
        <h1 class="heading">{title}</h1>
        <p class="message {message_class}">{message}</p>
        <p class="close-instruction">You can now close this window and return to the app</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@router.post("/v1/paypal/payment-details")
def save_paypal_payment_details(data: dict, uid: str = Depends(auth.get_current_user_uid)):
    """
    Save PayPal payment details (email and paypal.me link)
    """
    try:
        paypalme_url = data.get('paypalme_url')
        if paypalme_url and not paypalme_url.startswith('http'):
            paypalme_url = 'https://' + paypalme_url
        data['paypalme_url'] = paypalme_url
        set_paypal_payment_details(uid, data)
        if get_default_payment_method(uid) is None:
            set_default_payment_method(uid, 'paypal')
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/v1/paypal/payment-details")
def get_paypal_payment_details_endpoint(uid: str = Depends(auth.get_current_user_uid)):
    """
    Get the PayPal payment details for the user
    """
    details = get_paypal_payment_details(uid)
    # remove the starting https:// from the paypalme_url
    if details:
        details['paypalme_url'] = details.get('paypalme_url', '').replace('https://', '')
    return details


@router.get("/v1/payment-methods/status")
def get_payment_method_status(uid: str = Depends(auth.get_current_user_uid)):
    """Get the statuses of the payment methods for the user"""
    default_payment_method = get_default_payment_method(uid)

    # Check Stripe status
    stripe_account_id = get_stripe_connect_account_id(uid)
    stripe_status = 'not_connected'
    if stripe_account_id:
        stripe_status = 'connected' if is_onboarding_complete(stripe_account_id) else 'incomplete'

    # Check PayPal status
    paypal_status = 'connected' if get_paypal_payment_details(uid) else 'not_connected'

    return {
        "stripe": stripe_status,
        "paypal": paypal_status,
        "default": default_payment_method
    }


@router.post("/v1/payment-methods/default")
def set_default_payment_method_endpoint(data: dict, uid: str = Depends(auth.get_current_user_uid)):
    """Set the default payment method for the user"""
    method = data.get('method')
    if method not in ['stripe', 'paypal']:
        raise HTTPException(status_code=400, detail="Invalid method")
    set_default_payment_method(uid, method)
    return {"status": "success"}
