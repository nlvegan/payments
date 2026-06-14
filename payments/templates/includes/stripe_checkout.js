// Stripe Checkout with PaymentIntent API
// Supports 3D Secure / SCA (Strong Customer Authentication)

const stripe = Stripe("{{ publishable_key }}");
const paymentData = {{ payment_data }};

// Create Stripe Elements
const elements = stripe.elements();

const style = {
	base: {
		color: '#32325d',
		fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
		fontSmoothing: 'antialiased',
		fontSize: '16px',
		'::placeholder': {
			color: '#aab7c4'
		}
	},
	invalid: {
		color: '#dc3545',
		iconColor: '#dc3545'
	}
};

const card = elements.create('card', {
	hidePostalCode: true,
	style: style
});

card.mount('#card-element');

// Handle real-time validation errors from the card Element
card.on('change', function(event) {
	const displayError = document.getElementById('card-errors');
	if (event.error) {
		displayError.textContent = event.error.message;
		displayError.style.display = 'block';
	} else {
		displayError.textContent = '';
		displayError.style.display = 'none';
	}
});

// Handle form submission
frappe.ready(function() {
	const form = document.getElementById('payment-form');
	const submitButton = document.getElementById('submit-button');
	const spinner = document.getElementById('spinner');
	const buttonText = document.getElementById('button-text');

	form.addEventListener('submit', async function(event) {
		event.preventDefault();

		// Disable the submit button and show loading state
		setLoading(true);

		try {
			// Step 1: Create PaymentIntent on server
			const intentResponse = await frappe.call({
				method: 'payments.templates.pages.stripe_checkout.create_payment_intent',
				args: {
					data: JSON.stringify({
						...paymentData,
						payer_name: document.getElementById('cardholder-name').value,
						payer_email: document.getElementById('cardholder-email').value,
					})
				},
				async: true,
			});

			if (!intentResponse.message || !intentResponse.message.client_secret) {
				throw new Error(__('Failed to initialize payment. Please try again.'));
			}

			const clientSecret = intentResponse.message.client_secret;
			const paymentIntentId = intentResponse.message.payment_intent_id;

			// Step 2: Confirm payment with Stripe.js
			// This will handle 3D Secure automatically
			const billingDetails = {
				name: document.getElementById('cardholder-name').value,
				email: document.getElementById('cardholder-email').value,
			};

			// Add billing address if fields exist
			const addressLine1 = document.getElementById('billing-address-line1');
			if (addressLine1 && addressLine1.value) {
				billingDetails.address = {
					line1: addressLine1.value,
					line2: document.getElementById('billing-address-line2')?.value || '',
					city: document.getElementById('billing-city')?.value || '',
					state: document.getElementById('billing-state')?.value || '',
					postal_code: document.getElementById('billing-postal-code')?.value || '',
					country: document.getElementById('billing-country')?.value || '',
				};
			}

			const { error, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
				payment_method: {
					card: card,
					billing_details: billingDetails
				}
			});

			if (error) {
				// Show error to customer
				showError(error.message);
				setLoading(false);
				return;
			}

			// Step 3: Handle the result
			if (paymentIntent.status === 'succeeded') {
				// Payment successful
				showSuccess();

				// Confirm with server and get redirect URL
				const confirmResponse = await frappe.call({
					method: 'payments.templates.pages.stripe_checkout.confirm_payment',
					args: {
						payment_intent_id: paymentIntentId,
						reference_doctype: paymentData.reference_doctype,
						reference_docname: paymentData.reference_docname,
					},
					async: true,
				});

				if (confirmResponse.message && confirmResponse.message.redirect_to) {
					setTimeout(function() {
						window.location.href = confirmResponse.message.redirect_to;
					}, 1500);
				}

			} else if (paymentIntent.status === 'processing') {
				// Payment is processing (async payment methods)
				showMessage(__('Payment is being processed. You will receive a confirmation shortly.'), 'warning');
				setTimeout(function() {
					window.location.href = '/payment-success?status=processing&doctype=' +
						paymentData.reference_doctype + '&docname=' + paymentData.reference_docname;
				}, 2000);

			} else if (paymentIntent.status === 'requires_action') {
				// This shouldn't happen as confirmCardPayment handles 3DS
				// But just in case...
				showError(__('Additional authentication required. Please try again.'));
				setLoading(false);

			} else {
				// Payment failed
				showError(__('Payment was not successful. Please try again.'));
				setLoading(false);
			}

		} catch (error) {
			console.error('Payment error:', error);
			showError(error.message || __('An error occurred. Please try again.'));
			setLoading(false);
		}
	});
});

function setLoading(isLoading) {
	const submitButton = document.getElementById('submit-button');
	const spinner = document.getElementById('spinner');
	const buttonText = document.getElementById('button-text');

	if (isLoading) {
		submitButton.disabled = true;
		spinner.classList.remove('hidden');
		buttonText.classList.add('hidden');
	} else {
		submitButton.disabled = false;
		spinner.classList.add('hidden');
		buttonText.classList.remove('hidden');
	}
}

function showError(message) {
	const errorDiv = document.getElementById('payment-message');
	errorDiv.textContent = message;
	errorDiv.className = 'payment-message error';
	errorDiv.style.display = 'block';

	// Hide after 5 seconds
	setTimeout(function() {
		errorDiv.style.display = 'none';
	}, 5000);
}

function showSuccess() {
	const errorDiv = document.getElementById('payment-message');
	errorDiv.textContent = __('Payment successful! Redirecting...');
	errorDiv.className = 'payment-message success';
	errorDiv.style.display = 'block';

	// Hide the form
	document.getElementById('payment-form').style.display = 'none';
}

function showMessage(message, type) {
	const messageDiv = document.getElementById('payment-message');
	messageDiv.textContent = message;
	messageDiv.className = 'payment-message ' + (type || 'info');
	messageDiv.style.display = 'block';
}
