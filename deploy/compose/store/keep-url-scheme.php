<?php
/**
 * Plugin Name: Keep URL scheme
 * Description: Our MCP server tells WordPress "this request came over HTTPS" (X-Forwarded-Proto) so that
 *              WooCommerce accepts consumer-key authentication on the in-cluster HTTP hop. A side effect
 *              is that WordPress then builds every URL with https://, including product links in API
 *              responses, which cannot be opened on an HTTP-only site. This keeps generated URLs on the
 *              scheme of the configured site address (WP_HOME): http locally, https behind a TLS ingress.
 *              (Verified against WordPress 7.1.3 get_home_url(): is_ssl() forces https when no scheme is given.)
 */
if (!defined('ABSPATH')) {
    exit;
}

function wcmcp_keep_site_scheme($url, $path, $orig_scheme) {
    // Only when the caller did not ask for a specific scheme ('rest' means "whatever the request uses").
    if (null !== $orig_scheme && 'rest' !== $orig_scheme) {
        return $url;
    }
    $configured = parse_url(get_option('home'), PHP_URL_SCHEME);
    return $configured ? set_url_scheme($url, $configured) : $url;
}
add_filter('home_url', 'wcmcp_keep_site_scheme', 10, 3);
add_filter('site_url', 'wcmcp_keep_site_scheme', 10, 3);
