// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stripe Settings", {
  refresh: function (frm) {
    // Display the webhook endpoint URL from onload data
    if (frm.doc.__onload && frm.doc.__onload.webhook_endpoint_url) {
      frm.set_value(
        "webhook_endpoint_url",
        frm.doc.__onload.webhook_endpoint_url
      );
    }
  },
});
