<?php
/**
 * Registers a read/write WooCommerce REST API key whose value WE chose (from the environment),
 * instead of one generated in the admin screen. WooCommerce stores the key hashed and the secret as is.
 * Idempotent: running again with the same key updates the secret; with a new key, adds it.
 * Run with:  wp eval-file create-api-key.php   (WooCommerce must be active)
 */
global $wpdb;

$key    = getenv('WC_CONSUMER_KEY');
$secret = getenv('WC_CONSUMER_SECRET');
$login  = getenv('WP_ADMIN_USER');
if (!$key || !$secret || !$login) {
    fwrite(STDERR, "WC_CONSUMER_KEY, WC_CONSUMER_SECRET and WP_ADMIN_USER must be set\n");
    exit(1);
}
$user = get_user_by('login', $login);
if (!$user) {
    fwrite(STDERR, "no WordPress user named $login\n");
    exit(1);
}

$table = $wpdb->prefix . 'woocommerce_api_keys';
$hash  = wc_api_hash($key);
$id    = $wpdb->get_var($wpdb->prepare("SELECT key_id FROM $table WHERE consumer_key = %s", $hash));

$row = array(
    'user_id'         => $user->ID,
    'description'     => 'wc-mcp-gateway',
    'permissions'     => 'read_write',
    'consumer_key'    => $hash,
    'consumer_secret' => $secret,
    'truncated_key'   => substr($key, -7),
);
if ($id) {
    $ok = $wpdb->update($table, $row, array('key_id' => $id));
    echo "updated existing API key #$id\n";
} else {
    $ok = $wpdb->insert($table, $row);
    echo "inserted API key #" . $wpdb->insert_id . "\n";
}
if (false === $ok) {
    fwrite(STDERR, "database error: " . $wpdb->last_error . "\n");
    exit(1);
}
