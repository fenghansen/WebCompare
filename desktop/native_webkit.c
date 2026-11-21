/* Inject the private credential only inside the WebProcess, never JavaScript. */
#include <webkit2/webkit-web-extension.h>
#include <libsoup/soup.h>
#include <string.h>

#define PRIVATE_HEADER "X-WebCompare-Desktop"

typedef struct {
    GUri *origin;
    gchar *token;
} DesktopAuth;

static gboolean same_origin(const gchar *url, const DesktopAuth *auth)
{
    GUri *uri = g_uri_parse(url, G_URI_FLAGS_NONE, NULL);
    if (!uri)
        return FALSE;
    const gchar *scheme = g_uri_get_scheme(uri);
    const gchar *host = g_uri_get_host(uri);
    gint port = g_uri_get_port(uri);
    if (port == -1)
        port = 80;
    gboolean trusted = scheme && host && !g_uri_get_userinfo(uri) &&
        g_ascii_strcasecmp(scheme, "http") == 0 &&
        g_ascii_strcasecmp(host, g_uri_get_host(auth->origin)) == 0 &&
        port == g_uri_get_port(auth->origin);
    g_uri_unref(uri);
    return trusted;
}

static gboolean before_request(WebKitWebPage *page, WebKitURIRequest *request,
                               WebKitURIResponse *redirected_response, gpointer data)
{
    (void)page;
    (void)redirected_response;
    DesktopAuth *auth = data;
    SoupMessageHeaders *headers = webkit_uri_request_get_http_headers(request);
    if (headers) {
        soup_message_headers_remove(headers, PRIVATE_HEADER);
        if (same_origin(webkit_uri_request_get_uri(request), auth))
            soup_message_headers_append(headers, PRIVATE_HEADER, auth->token);
    }
    return FALSE;
}

static void page_created(WebKitWebExtension *extension, WebKitWebPage *page, gpointer data)
{
    (void)extension;
    g_signal_connect(page, "send-request", G_CALLBACK(before_request), data);
}

G_MODULE_EXPORT void webkit_web_extension_initialize_with_user_data(
    WebKitWebExtension *extension, const GVariant *user_data)
{
    if (!user_data || !g_variant_is_of_type((GVariant *)user_data, G_VARIANT_TYPE("(ss)")))
        return;
    const gchar *origin, *token;
    g_variant_get((GVariant *)user_data, "(&s&s)", &origin, &token);
    GUri *uri = g_uri_parse(origin, G_URI_FLAGS_NONE, NULL);
    if (!uri || !g_uri_get_scheme(uri) || !g_uri_get_host(uri) ||
        strcmp(g_uri_get_scheme(uri), "http") != 0 ||
        strcmp(g_uri_get_host(uri), "127.0.0.1") != 0 ||
        g_uri_get_userinfo(uri) || g_uri_get_port(uri) < 1 || !*token) {
        if (uri)
            g_uri_unref(uri);
        return;
    }
    DesktopAuth *auth = g_new0(DesktopAuth, 1);
    auth->origin = uri;
    auth->token = g_strdup(token);
    g_signal_connect(extension, "page-created", G_CALLBACK(page_created), auth);
}
