<?php
// Include the Composer autoloader
require_once __DIR__ . '/vendor/autoload.php';

// Use the correct namespace for PaymentIntent
use Paymongo\Entities\PaymentIntent;
use Paymongo\PaymongoClient;  // Assuming this is the API client for Paymongo

// Create a Paymongo client (assuming such a client exists for interacting with the API)
$paymongoClient = new PaymongoClient();

// Use the PaymongoClient to create a PaymentIntent (Check the API documentation for the correct function)
$response = $paymongoClient->paymentIntents->create([
    'amount' => 10000,  // Amount in cents (10000 cents = 100 PHP)
    'currency' => 'PHP',
    'payment_method_allowed' => ['gcash', 'card']
]);

// Print the payment intent details
print_r($response);
?>