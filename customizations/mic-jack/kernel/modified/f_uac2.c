// SPDX-License-Identifier: GPL-2.0+
/*
 * f_uac2.c -- USB Audio Class 2.0 Function
 *
 * Copyright (C) 2011
 *    Yadwinder Singh (yadi.brar01@gmail.com)
 *    Jaswinder Singh (jaswinder.singh@linaro.org)
 */

#include <linux/usb/audio.h>
#include <linux/usb/audio-v2.h>
#include <linux/module.h>
#include <linux/kref.h>
#include <linux/spinlock.h>

#include "u_audio.h"
#include "u_uac2.h"

// ### SIPEED EDIT ###
/* UAC2 spec: 4.1 Audio Channel Cluster Descriptor */
#define UAC2_CHANNEL_MASK 0x07FFFFFF
// ### SIPEED EDIT END ###

/*
 * The driver implements a simple UAC_2 topology.
 * USB-OUT -> IT_1 -> OT_3 -> ALSA_Capture
 * ALSA_Playback -> IT_2 -> OT_4 -> USB-IN
 * Capture and Playback sampling rates are independently
 *  controlled by two clock sources :
 *    CLK_5 := c_srate, and CLK_6 := p_srate
 */
// ### SIPEED EDIT ###
#define USB_OUT_CLK_ID	(out_clk_src_desc.bClockID)
#define USB_IN_CLK_ID	(in_clk_src_desc.bClockID)
// ### SIPEED EDIT END ###

#define CONTROL_ABSENT	0
#define CONTROL_RDONLY	1
#define CONTROL_RDWR	3

#define CLK_FREQ_CTRL	0
#define CLK_VLD_CTRL	2

#define COPY_CTRL	0
#define CONN_CTRL	2
#define OVRLD_CTRL	4
#define CLSTR_CTRL	6
#define UNFLW_CTRL	8
#define OVFLW_CTRL	10

// ### SIPEED EDIT ###
#define EPIN_EN(_opts) ((_opts)->p_chmask != 0)
#define EPOUT_EN(_opts) ((_opts)->c_chmask != 0)
// ### SIPEED EDIT END ###

/*
 * One bounded, coalescing notification request per bound function.  The owner
 * and each successful queue hold a reference; completion never accesses the
 * usb_function.  A late completion after a failed endpoint disable does not
 * dereference f_uac2; this does not extend the lifetime of the UDC/usb_ep.
 */
struct uac2_jack_notify {
	struct kref ref;
	spinlock_t lock;
	struct usb_ep *ep;
	struct usb_request *req;
	bool enabled;
	bool busy;
	bool pending;
};

struct f_uac2 {
	struct g_audio g_audio;
	u8 ac_intf, as_in_intf, as_out_intf;
	u8 ac_alt, as_in_alt, as_out_alt;	/* needed for get_alt() */
	/* opts->lock protects list membership and the notify pointer. */
	struct list_head jack_node;
	struct uac2_jack_notify *jack_notify;
	bool p_jack_detect;
	bool p_jack_connected;
	u8 p_it_id;
	u32 p_chmask;
};

static inline struct f_uac2 *func_to_uac2(struct usb_function *f)
{
	return container_of(f, struct f_uac2, g_audio.func);
}

static inline
struct f_uac2_opts *g_audio_to_uac2_opts(struct g_audio *agdev)
{
	return container_of(agdev->func.fi, struct f_uac2_opts, func_inst);
}

static void uac2_jack_release(struct kref *ref)
{
	struct uac2_jack_notify *notify =
		container_of(ref, struct uac2_jack_notify, ref);

	kfree(notify->req->buf);
	usb_ep_free_request(notify->ep, notify->req);
	kfree(notify);
}

/* notify->lock held with IRQs disabled.  usb_ep_queue cannot call complete. */
static int uac2_jack_queue_locked(struct uac2_jack_notify *notify)
{
	int ret;

	if (!notify->enabled || notify->busy || !notify->pending)
		return 0;

	notify->busy = true;
	notify->pending = false;
	kref_get(&notify->ref);
	ret = usb_ep_queue(notify->ep, notify->req, GFP_ATOMIC);
	if (ret) {
		notify->busy = false;
		notify->pending = true;
		/* The owner or the invoking completion still holds a reference. */
		kref_put(&notify->ref, uac2_jack_release);
	}
	return ret;
}

static void uac2_jack_complete(struct usb_ep *ep, struct usb_request *req)
{
	struct uac2_jack_notify *notify = req->context;
	unsigned long flags;
	int ret = 0;

	spin_lock_irqsave(&notify->lock, flags);
	notify->busy = false;
	if (req->status) {
		/* No immediate error retry loop.  A write or set_alt retries. */
		notify->pending = true;
	} else {
		ret = uac2_jack_queue_locked(notify);
	}
	spin_unlock_irqrestore(&notify->lock, flags);
	if (ret)
		pr_warn_ratelimited("uac2: connector notification queue: %d\n",
				    ret);
	kref_put(&notify->ref, uac2_jack_release);
}

static struct uac2_jack_notify *uac2_jack_alloc(struct usb_ep *ep,
					      u8 terminal, u8 intf)
{
	struct uac2_jack_notify *notify;
	struct uac2_interrupt_data_msg *msg;

	notify = kzalloc(sizeof(*notify), GFP_KERNEL);
	if (!notify)
		return NULL;
	notify->ep = ep;
	notify->req = usb_ep_alloc_request(ep, GFP_KERNEL);
	if (!notify->req)
		goto err_notify;
	msg = kzalloc(sizeof(*msg), GFP_KERNEL);
	if (!msg)
		goto err_request;

	/* UAC2 6.1: standard interface notification, CUR has changed. */
	msg->bInfo = 0;
	msg->bAttribute = UAC2_CS_CUR;
	msg->wValue = cpu_to_le16(UAC2_TE_CONNECTOR << 8);
	msg->wIndex = cpu_to_le16((terminal << 8) | intf);
	notify->req->buf = msg;
	notify->req->length = sizeof(*msg);
	notify->req->zero = 0;
	notify->req->context = notify;
	notify->req->complete = uac2_jack_complete;
	notify->pending = true;
	spin_lock_init(&notify->lock);
	kref_init(&notify->ref);
	return notify;

err_request:
	usb_ep_free_request(ep, notify->req);
err_notify:
	kfree(notify);
	return NULL;
}

static int uac2_jack_stop(struct uac2_jack_notify *notify)
{
	unsigned long flags;
	int ret;

	if (!notify)
		return 0;
	/* Serialize with producers, but never hold this lock over disable. */
	spin_lock_irqsave(&notify->lock, flags);
	notify->enabled = false;
	notify->pending = true;
	spin_unlock_irqrestore(&notify->lock, flags);
	/* On success the 4.19 gadget API drains all completion callbacks. */
	ret = usb_ep_disable(notify->ep);
	if (ret)
		pr_err("uac2: connector endpoint disable failed: %d\n", ret);
	return ret;
}

static int uac2_jack_start(struct usb_function *fn)
{
	struct uac2_jack_notify *notify = func_to_uac2(fn)->jack_notify;
	unsigned long flags;
	int ret;

	if (!notify)
		return 0;
	ret = uac2_jack_stop(notify);
	if (ret)
		return ret;
	ret = config_ep_by_speed(fn->config->cdev->gadget, fn, notify->ep);
	if (ret)
		return ret;
	ret = usb_ep_enable(notify->ep);
	if (ret)
		return ret;

	spin_lock_irqsave(&notify->lock, flags);
	notify->enabled = true;
	notify->pending = true;
	ret = uac2_jack_queue_locked(notify);
	spin_unlock_irqrestore(&notify->lock, flags);
	if (ret)
		uac2_jack_stop(notify);
	return ret;
}

/* Called with opts->lock held, excluding unbind/free of this function. */
static void uac2_jack_set_connected(struct f_uac2 *uac2, bool connected)
{
	struct uac2_jack_notify *notify = uac2->jack_notify;
	bool changed = READ_ONCE(uac2->p_jack_connected) != connected;
	unsigned long flags;
	int ret;

	WRITE_ONCE(uac2->p_jack_connected, connected);
	if (!notify)
		return;
	spin_lock_irqsave(&notify->lock, flags);
	if (changed)
		notify->pending = true;
	/* Rewriting the same value also retries a previously failed queue. */
	ret = uac2_jack_queue_locked(notify);
	spin_unlock_irqrestore(&notify->lock, flags);
	if (ret)
		pr_warn_ratelimited("uac2: connector notification queue: %d\n",
				    ret);
}

/* --------- USB Function Interface ------------- */

enum {
	STR_ASSOC,
	STR_IF_CTRL,
	STR_CLKSRC_IN,
	STR_CLKSRC_OUT,
	STR_USB_IT,
	STR_IO_IT,
	STR_USB_OT,
	STR_IO_OT,
	STR_AS_OUT_ALT0,
	STR_AS_OUT_ALT1,
	STR_AS_IN_ALT0,
	STR_AS_IN_ALT1,
};

static char clksrc_in[8];
static char clksrc_out[8];

static struct usb_string strings_fn[] = {
	// ### SIPEED EDIT ###
	/* [STR_ASSOC].s = DYNAMIC, */
	// ### SIPEED EDIT END ###
	[STR_IF_CTRL].s = "Topology Control",
	[STR_CLKSRC_IN].s = clksrc_in,
	[STR_CLKSRC_OUT].s = clksrc_out,
	[STR_USB_IT].s = "USBH Out",
	[STR_IO_IT].s = "USBD Out",
	[STR_USB_OT].s = "USBH In",
	[STR_IO_OT].s = "USBD In",
	[STR_AS_OUT_ALT0].s = "Playback Inactive",
	[STR_AS_OUT_ALT1].s = "Playback Active",
	[STR_AS_IN_ALT0].s = "Capture Inactive",
	[STR_AS_IN_ALT1].s = "Capture Active",
	{ },
};

static struct usb_gadget_strings str_fn = {
	.language = 0x0409,	/* en-us */
	.strings = strings_fn,
};

static struct usb_gadget_strings *fn_strings[] = {
	&str_fn,
	NULL,
};

static struct usb_interface_assoc_descriptor iad_desc = {
	.bLength = sizeof iad_desc,
	.bDescriptorType = USB_DT_INTERFACE_ASSOCIATION,

	.bFirstInterface = 0,
	.bInterfaceCount = 3,
	.bFunctionClass = USB_CLASS_AUDIO,
	.bFunctionSubClass = UAC2_FUNCTION_SUBCLASS_UNDEFINED,
	.bFunctionProtocol = UAC_VERSION_2,
};

/* Audio Control Interface */
static struct usb_interface_descriptor std_ac_if_desc = {
	.bLength = sizeof std_ac_if_desc,
	.bDescriptorType = USB_DT_INTERFACE,

	.bAlternateSetting = 0,
	.bNumEndpoints = 0,
	.bInterfaceClass = USB_CLASS_AUDIO,
	.bInterfaceSubClass = USB_SUBCLASS_AUDIOCONTROL,
	.bInterfaceProtocol = UAC_VERSION_2,
};

/* Clock source for IN traffic */
static struct uac_clock_source_descriptor in_clk_src_desc = {
	.bLength = sizeof in_clk_src_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC2_CLOCK_SOURCE,
	// ### SIPEED EDIT ###
	/* .bClockID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmAttributes = UAC_CLOCK_SOURCE_TYPE_INT_FIXED,
	.bmControls = (CONTROL_RDONLY << CLK_FREQ_CTRL),
	.bAssocTerminal = 0,
};

/* Clock source for OUT traffic */
static struct uac_clock_source_descriptor out_clk_src_desc = {
	.bLength = sizeof out_clk_src_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC2_CLOCK_SOURCE,
	// ### SIPEED EDIT ###
	/* .bClockID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmAttributes = UAC_CLOCK_SOURCE_TYPE_INT_FIXED,
	.bmControls = (CONTROL_RDONLY << CLK_FREQ_CTRL),
	.bAssocTerminal = 0,
};

/* Input Terminal for USB_OUT */
static struct uac2_input_terminal_descriptor usb_out_it_desc = {
	.bLength = sizeof usb_out_it_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_INPUT_TERMINAL,
	// ### SIPEED EDIT ###
	/* .bTerminalID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.wTerminalType = cpu_to_le16(UAC_TERMINAL_STREAMING),
	.bAssocTerminal = 0,
	// ### SIPEED EDIT ###
	/* .bCSourceID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.iChannelNames = 0,
	.bmControls = cpu_to_le16(CONTROL_RDWR << COPY_CTRL),
};

/* Input Terminal for I/O-In */
static struct uac2_input_terminal_descriptor io_in_it_desc = {
	.bLength = sizeof io_in_it_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_INPUT_TERMINAL,
	// ### SIPEED EDIT ###
	/* .bTerminalID = DYNAMIC */
	/* .wTerminalType = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bAssocTerminal = 0,
	// ### SIPEED EDIT ###
	/* .bCSourceID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.iChannelNames = 0,
	.bmControls = cpu_to_le16(CONTROL_RDWR << COPY_CTRL),
};

/* Ouput Terminal for USB_IN */
static struct uac2_output_terminal_descriptor usb_in_ot_desc = {
	.bLength = sizeof usb_in_ot_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_OUTPUT_TERMINAL,
	// ### SIPEED EDIT ###
	/* .bTerminalID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.wTerminalType = cpu_to_le16(UAC_TERMINAL_STREAMING),
	.bAssocTerminal = 0,
	// ### SIPEED EDIT ###
	/* .bSourceID = DYNAMIC */
	/* .bCSourceID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmControls = cpu_to_le16(CONTROL_RDWR << COPY_CTRL),
};

/* Ouput Terminal for I/O-Out */
static struct uac2_output_terminal_descriptor io_out_ot_desc = {
	.bLength = sizeof io_out_ot_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_OUTPUT_TERMINAL,
	// ### SIPEED EDIT ###
	/* .bTerminalID = DYNAMIC */
	/* .wTerminalType = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bAssocTerminal = 0,
	// ### SIPEED EDIT ###
	/* .bSourceID = DYNAMIC */
	/* .bCSourceID = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmControls = cpu_to_le16(CONTROL_RDWR << COPY_CTRL),
};

static struct uac2_ac_header_descriptor ac_hdr_desc = {
	.bLength = sizeof ac_hdr_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_MS_HEADER,
	.bcdADC = cpu_to_le16(0x200),
	.bCategory = UAC2_FUNCTION_IO_BOX,
	// ### SIPEED EDIT ###
	/* .wTotalLength = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmControls = 0,
};

/* Audio Streaming OUT Interface - Alt0 */
static struct usb_interface_descriptor std_as_out_if0_desc = {
	.bLength = sizeof std_as_out_if0_desc,
	.bDescriptorType = USB_DT_INTERFACE,

	.bAlternateSetting = 0,
	.bNumEndpoints = 0,
	.bInterfaceClass = USB_CLASS_AUDIO,
	.bInterfaceSubClass = USB_SUBCLASS_AUDIOSTREAMING,
	.bInterfaceProtocol = UAC_VERSION_2,
};

/* Audio Streaming OUT Interface - Alt1 */
static struct usb_interface_descriptor std_as_out_if1_desc = {
	.bLength = sizeof std_as_out_if1_desc,
	.bDescriptorType = USB_DT_INTERFACE,

	.bAlternateSetting = 1,
	.bNumEndpoints = 1,
	.bInterfaceClass = USB_CLASS_AUDIO,
	.bInterfaceSubClass = USB_SUBCLASS_AUDIOSTREAMING,
	.bInterfaceProtocol = UAC_VERSION_2,
};

/* Audio Stream OUT Intface Desc */
static struct uac2_as_header_descriptor as_out_hdr_desc = {
	.bLength = sizeof as_out_hdr_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_AS_GENERAL,
	// ### SIPEED EDIT ###
	/* .bTerminalLink = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmControls = 0,
	.bFormatType = UAC_FORMAT_TYPE_I,
	.bmFormats = cpu_to_le32(UAC_FORMAT_TYPE_I_PCM),
	.iChannelNames = 0,
};

/* Audio USB_OUT Format */
static struct uac2_format_type_i_descriptor as_out_fmt1_desc = {
	.bLength = sizeof as_out_fmt1_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,
	.bDescriptorSubtype = UAC_FORMAT_TYPE,
	.bFormatType = UAC_FORMAT_TYPE_I,
};

/* STD AS ISO OUT Endpoint */
static struct usb_endpoint_descriptor fs_epout_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bEndpointAddress = USB_DIR_OUT,
	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	// ### SIPEED EDIT ###
	/* .wMaxPacketSize = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bInterval = 1,
};

static struct usb_endpoint_descriptor hs_epout_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	// ### SIPEED EDIT ###
	/* .wMaxPacketSize = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bInterval = 4,
};

// ### SIPEED EDIT ###
static struct usb_endpoint_descriptor ss_epout_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bEndpointAddress = USB_DIR_OUT,
	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	/* .wMaxPacketSize = DYNAMIC */
	.bInterval = 4,
};

static struct usb_ss_ep_comp_descriptor ss_epout_desc_comp = {
	.bLength		= sizeof(ss_epout_desc_comp),
	.bDescriptorType	= USB_DT_SS_ENDPOINT_COMP,
	.bMaxBurst		= 0,
	.bmAttributes		= 0,
	/* wBytesPerInterval = DYNAMIC */
};
// ### SIPEED EDIT END ###

/* CS AS ISO OUT Endpoint */
static struct uac2_iso_endpoint_descriptor as_iso_out_desc = {
	.bLength = sizeof as_iso_out_desc,
	.bDescriptorType = USB_DT_CS_ENDPOINT,

	.bDescriptorSubtype = UAC_EP_GENERAL,
	.bmAttributes = 0,
	.bmControls = 0,
	.bLockDelayUnits = 0,
	.wLockDelay = 0,
};

/* Audio Streaming IN Interface - Alt0 */
static struct usb_interface_descriptor std_as_in_if0_desc = {
	.bLength = sizeof std_as_in_if0_desc,
	.bDescriptorType = USB_DT_INTERFACE,

	.bAlternateSetting = 0,
	.bNumEndpoints = 0,
	.bInterfaceClass = USB_CLASS_AUDIO,
	.bInterfaceSubClass = USB_SUBCLASS_AUDIOSTREAMING,
	.bInterfaceProtocol = UAC_VERSION_2,
};

/* Audio Streaming IN Interface - Alt1 */
static struct usb_interface_descriptor std_as_in_if1_desc = {
	.bLength = sizeof std_as_in_if1_desc,
	.bDescriptorType = USB_DT_INTERFACE,

	.bAlternateSetting = 1,
	.bNumEndpoints = 1,
	.bInterfaceClass = USB_CLASS_AUDIO,
	.bInterfaceSubClass = USB_SUBCLASS_AUDIOSTREAMING,
	.bInterfaceProtocol = UAC_VERSION_2,
};

/* Audio Stream IN Intface Desc */
static struct uac2_as_header_descriptor as_in_hdr_desc = {
	.bLength = sizeof as_in_hdr_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,

	.bDescriptorSubtype = UAC_AS_GENERAL,
	// ### SIPEED EDIT ###
	/* .bTerminalLink = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bmControls = 0,
	.bFormatType = UAC_FORMAT_TYPE_I,
	.bmFormats = cpu_to_le32(UAC_FORMAT_TYPE_I_PCM),
	.iChannelNames = 0,
};

/* Audio USB_IN Format */
static struct uac2_format_type_i_descriptor as_in_fmt1_desc = {
	.bLength = sizeof as_in_fmt1_desc,
	.bDescriptorType = USB_DT_CS_INTERFACE,
	.bDescriptorSubtype = UAC_FORMAT_TYPE,
	.bFormatType = UAC_FORMAT_TYPE_I,
};

/* STD AS ISO IN Endpoint */
static struct usb_endpoint_descriptor fs_epin_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bEndpointAddress = USB_DIR_IN,
	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	// ### SIPEED EDIT ###
	/* .wMaxPacketSize = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bInterval = 1,
};

static struct usb_endpoint_descriptor hs_epin_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	// ### SIPEED EDIT ###
	/* .wMaxPacketSize = DYNAMIC */
	// ### SIPEED EDIT END ###
	.bInterval = 4,
};

// ### SIPEED EDIT ###
static struct usb_endpoint_descriptor ss_epin_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,

	.bEndpointAddress = USB_DIR_IN,
	.bmAttributes = USB_ENDPOINT_XFER_ISOC | USB_ENDPOINT_SYNC_ASYNC,
	/* .wMaxPacketSize = DYNAMIC */
	.bInterval = 4,
};

static struct usb_ss_ep_comp_descriptor ss_epin_desc_comp = {
	.bLength		= sizeof(ss_epin_desc_comp),
	.bDescriptorType	= USB_DT_SS_ENDPOINT_COMP,
	.bMaxBurst		= 0,
	.bmAttributes		= 0,
	/* wBytesPerInterval = DYNAMIC */
};
// ### SIPEED EDIT END ###

/* CS AS ISO IN Endpoint */
static struct uac2_iso_endpoint_descriptor as_iso_in_desc = {
	.bLength = sizeof as_iso_in_desc,
	.bDescriptorType = USB_DT_CS_ENDPOINT,

	.bDescriptorSubtype = UAC_EP_GENERAL,
	.bmAttributes = 0,
	.bmControls = 0,
	.bLockDelayUnits = 0,
	.wLockDelay = 0,
};

/* Optional AudioControl interrupt IN endpoint, UAC2 6.1. */
static struct usb_endpoint_descriptor fs_jack_ep_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = USB_DIR_IN,
	.bmAttributes = USB_ENDPOINT_XFER_INT,
	.wMaxPacketSize = cpu_to_le16(6),
	.bInterval = 1,
};

static struct usb_endpoint_descriptor hs_jack_ep_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = USB_DIR_IN,
	.bmAttributes = USB_ENDPOINT_XFER_INT,
	.wMaxPacketSize = cpu_to_le16(6),
	.bInterval = 4,
};

static struct usb_endpoint_descriptor ss_jack_ep_desc = {
	.bLength = USB_DT_ENDPOINT_SIZE,
	.bDescriptorType = USB_DT_ENDPOINT,
	.bEndpointAddress = USB_DIR_IN,
	.bmAttributes = USB_ENDPOINT_XFER_INT,
	.wMaxPacketSize = cpu_to_le16(6),
	.bInterval = 4,
};

static struct usb_ss_ep_comp_descriptor ss_jack_ep_comp = {
	.bLength = sizeof(ss_jack_ep_comp),
	.bDescriptorType = USB_DT_SS_ENDPOINT_COMP,
	.wBytesPerInterval = cpu_to_le16(6),
};

static struct usb_descriptor_header *fs_audio_desc[] = {
	(struct usb_descriptor_header *)&iad_desc,
	(struct usb_descriptor_header *)&std_ac_if_desc,

	(struct usb_descriptor_header *)&ac_hdr_desc,
	(struct usb_descriptor_header *)&in_clk_src_desc,
	(struct usb_descriptor_header *)&out_clk_src_desc,
	(struct usb_descriptor_header *)&usb_out_it_desc,
	(struct usb_descriptor_header *)&io_in_it_desc,
	(struct usb_descriptor_header *)&usb_in_ot_desc,
	(struct usb_descriptor_header *)&io_out_ot_desc,
	(struct usb_descriptor_header *)&fs_jack_ep_desc,

	(struct usb_descriptor_header *)&std_as_out_if0_desc,
	(struct usb_descriptor_header *)&std_as_out_if1_desc,

	(struct usb_descriptor_header *)&as_out_hdr_desc,
	(struct usb_descriptor_header *)&as_out_fmt1_desc,
	(struct usb_descriptor_header *)&fs_epout_desc,
	(struct usb_descriptor_header *)&as_iso_out_desc,

	(struct usb_descriptor_header *)&std_as_in_if0_desc,
	(struct usb_descriptor_header *)&std_as_in_if1_desc,

	(struct usb_descriptor_header *)&as_in_hdr_desc,
	(struct usb_descriptor_header *)&as_in_fmt1_desc,
	(struct usb_descriptor_header *)&fs_epin_desc,
	(struct usb_descriptor_header *)&as_iso_in_desc,
	NULL,
};

static struct usb_descriptor_header *hs_audio_desc[] = {
	(struct usb_descriptor_header *)&iad_desc,
	(struct usb_descriptor_header *)&std_ac_if_desc,

	(struct usb_descriptor_header *)&ac_hdr_desc,
	(struct usb_descriptor_header *)&in_clk_src_desc,
	(struct usb_descriptor_header *)&out_clk_src_desc,
	(struct usb_descriptor_header *)&usb_out_it_desc,
	(struct usb_descriptor_header *)&io_in_it_desc,
	(struct usb_descriptor_header *)&usb_in_ot_desc,
	(struct usb_descriptor_header *)&io_out_ot_desc,
	(struct usb_descriptor_header *)&hs_jack_ep_desc,

	(struct usb_descriptor_header *)&std_as_out_if0_desc,
	(struct usb_descriptor_header *)&std_as_out_if1_desc,

	(struct usb_descriptor_header *)&as_out_hdr_desc,
	(struct usb_descriptor_header *)&as_out_fmt1_desc,
	(struct usb_descriptor_header *)&hs_epout_desc,
	(struct usb_descriptor_header *)&as_iso_out_desc,

	(struct usb_descriptor_header *)&std_as_in_if0_desc,
	(struct usb_descriptor_header *)&std_as_in_if1_desc,

	(struct usb_descriptor_header *)&as_in_hdr_desc,
	(struct usb_descriptor_header *)&as_in_fmt1_desc,
	(struct usb_descriptor_header *)&hs_epin_desc,
	(struct usb_descriptor_header *)&as_iso_in_desc,
	NULL,
};

// ### SIPEED EDIT ###
static struct usb_descriptor_header *ss_audio_desc[] = {
	(struct usb_descriptor_header *)&iad_desc,
	(struct usb_descriptor_header *)&std_ac_if_desc,

	(struct usb_descriptor_header *)&ac_hdr_desc,
	(struct usb_descriptor_header *)&in_clk_src_desc,
	(struct usb_descriptor_header *)&out_clk_src_desc,
	(struct usb_descriptor_header *)&usb_out_it_desc,
	(struct usb_descriptor_header *)&io_in_it_desc,
	(struct usb_descriptor_header *)&usb_in_ot_desc,
	(struct usb_descriptor_header *)&io_out_ot_desc,
	(struct usb_descriptor_header *)&ss_jack_ep_desc,
	(struct usb_descriptor_header *)&ss_jack_ep_comp,

	(struct usb_descriptor_header *)&std_as_out_if0_desc,
	(struct usb_descriptor_header *)&std_as_out_if1_desc,

	(struct usb_descriptor_header *)&as_out_hdr_desc,
	(struct usb_descriptor_header *)&as_out_fmt1_desc,
	(struct usb_descriptor_header *)&ss_epout_desc,
	(struct usb_descriptor_header *)&ss_epout_desc_comp,
	(struct usb_descriptor_header *)&as_iso_out_desc,

	(struct usb_descriptor_header *)&std_as_in_if0_desc,
	(struct usb_descriptor_header *)&std_as_in_if1_desc,

	(struct usb_descriptor_header *)&as_in_hdr_desc,
	(struct usb_descriptor_header *)&as_in_fmt1_desc,
	(struct usb_descriptor_header *)&ss_epin_desc,
	(struct usb_descriptor_header *)&ss_epin_desc_comp,
	(struct usb_descriptor_header *)&as_iso_in_desc,
	NULL,
};
// ### SIPEED EDIT END ###

struct cntrl_cur_lay3 {
	__le32	dCUR;
};

struct cntrl_range_lay3 {
	__le16	wNumSubRanges;
	__le32	dMIN;
	__le32	dMAX;
	__le32	dRES;
} __packed;

// ### SIPEED EDIT ###
static int set_ep_max_packet_size(const struct f_uac2_opts *uac2_opts,
	struct usb_endpoint_descriptor *ep_desc,
	enum usb_device_speed speed, bool is_playback)
{
	int chmask, srate, ssize;
	u16 max_size_bw, max_size_ep;
	unsigned int factor;

	switch (speed) {
	case USB_SPEED_FULL:
		max_size_ep = 1023;
		factor = 1000;
		break;

	case USB_SPEED_HIGH:
	case USB_SPEED_SUPER:
		max_size_ep = 1024;
		factor = 8000;
		break;

	default:
		return -EINVAL;
	}

	if (is_playback) {
		chmask = uac2_opts->p_chmask;
		srate = uac2_opts->p_srate;
		ssize = uac2_opts->p_ssize;
	} else {
		chmask = uac2_opts->c_chmask;
		srate = uac2_opts->c_srate;
		ssize = uac2_opts->c_ssize;
	}

	max_size_bw = num_channels(chmask) * ssize *
		((srate / (factor / (1 << (ep_desc->bInterval - 1)))) + 1);
	ep_desc->wMaxPacketSize = cpu_to_le16(min_t(u16, max_size_bw,
						    max_size_ep));

	return 0;
}
// ### SIPEED EDIT END ###

// ### SIPEED EDIT ###
/* Use macro to overcome line length limitation */
#define USBDHDR(p) (struct usb_descriptor_header *)(p)

static void setup_headers(struct f_uac2_opts *opts,
			  struct usb_descriptor_header **headers,
			  enum usb_device_speed speed)
{
	struct usb_ss_ep_comp_descriptor *epout_desc_comp = NULL;
	struct usb_ss_ep_comp_descriptor *epin_desc_comp = NULL;
	struct usb_endpoint_descriptor *epout_desc;
	struct usb_endpoint_descriptor *epin_desc;
	struct usb_endpoint_descriptor *jack_ep_desc;
	int i;

	switch (speed) {
	case USB_SPEED_FULL:
		epout_desc = &fs_epout_desc;
		epin_desc = &fs_epin_desc;
		jack_ep_desc = &fs_jack_ep_desc;
		break;
	case USB_SPEED_HIGH:
		epout_desc = &hs_epout_desc;
		epin_desc = &hs_epin_desc;
		jack_ep_desc = &hs_jack_ep_desc;
		break;
	default:
		epout_desc = &ss_epout_desc;
		epin_desc = &ss_epin_desc;
		jack_ep_desc = &ss_jack_ep_desc;
		epout_desc_comp = &ss_epout_desc_comp;
		epin_desc_comp = &ss_epin_desc_comp;
	}

	i = 0;
	headers[i++] = USBDHDR(&iad_desc);
	headers[i++] = USBDHDR(&std_ac_if_desc);
	headers[i++] = USBDHDR(&ac_hdr_desc);
	if (EPIN_EN(opts))
		headers[i++] = USBDHDR(&in_clk_src_desc);
	if (EPOUT_EN(opts)) {
		headers[i++] = USBDHDR(&out_clk_src_desc);
		headers[i++] = USBDHDR(&usb_out_it_desc);
	}
	if (EPIN_EN(opts)) {
		headers[i++] = USBDHDR(&io_in_it_desc);
		headers[i++] = USBDHDR(&usb_in_ot_desc);
	}
	if (EPOUT_EN(opts))
		headers[i++] = USBDHDR(&io_out_ot_desc);
	/* The interrupt endpoint belongs to AC, before either AS interface. */
	if (opts->p_jack_detect) {
		headers[i++] = USBDHDR(jack_ep_desc);
		if (speed == USB_SPEED_SUPER)
			headers[i++] = USBDHDR(&ss_jack_ep_comp);
	}
	if (EPOUT_EN(opts)) {
		headers[i++] = USBDHDR(&std_as_out_if0_desc);
		headers[i++] = USBDHDR(&std_as_out_if1_desc);
		headers[i++] = USBDHDR(&as_out_hdr_desc);
		headers[i++] = USBDHDR(&as_out_fmt1_desc);
		headers[i++] = USBDHDR(epout_desc);
		if (epout_desc_comp)
			headers[i++] = USBDHDR(epout_desc_comp);

		headers[i++] = USBDHDR(&as_iso_out_desc);
	}
	if (EPIN_EN(opts)) {
		headers[i++] = USBDHDR(&std_as_in_if0_desc);
		headers[i++] = USBDHDR(&std_as_in_if1_desc);
		headers[i++] = USBDHDR(&as_in_hdr_desc);
		headers[i++] = USBDHDR(&as_in_fmt1_desc);
		headers[i++] = USBDHDR(epin_desc);
		if (epin_desc_comp)
			headers[i++] = USBDHDR(epin_desc_comp);

		headers[i++] = USBDHDR(&as_iso_in_desc);
	}
	headers[i] = NULL;
}

static void setup_descriptor(struct f_uac2_opts *opts)
{
	/* patch descriptors */
	int i = 1; /* ID's start with 1 */

	if (EPOUT_EN(opts))
		usb_out_it_desc.bTerminalID = i++;
	if (EPIN_EN(opts))
		io_in_it_desc.bTerminalID = i++;
	if (EPOUT_EN(opts))
		io_out_ot_desc.bTerminalID = i++;
	if (EPIN_EN(opts))
		usb_in_ot_desc.bTerminalID = i++;
	if (EPOUT_EN(opts))
		out_clk_src_desc.bClockID = i++;
	if (EPIN_EN(opts))
		in_clk_src_desc.bClockID = i++;

	usb_out_it_desc.bCSourceID = out_clk_src_desc.bClockID;
	usb_in_ot_desc.bSourceID = io_in_it_desc.bTerminalID;
	usb_in_ot_desc.bCSourceID = in_clk_src_desc.bClockID;
	io_in_it_desc.bCSourceID = in_clk_src_desc.bClockID;
	io_out_ot_desc.bCSourceID = out_clk_src_desc.bClockID;
	io_out_ot_desc.bSourceID = usb_out_it_desc.bTerminalID;
	as_out_hdr_desc.bTerminalLink = usb_out_it_desc.bTerminalID;
	as_in_hdr_desc.bTerminalLink = usb_in_ot_desc.bTerminalID;

	/* Reset both fields on every bind, including detection-disabled binds. */
	std_ac_if_desc.bNumEndpoints = opts->p_jack_detect ? 1 : 0;
	io_in_it_desc.bmControls = cpu_to_le16((CONTROL_RDWR << COPY_CTRL) |
		(opts->p_jack_detect ? CONTROL_RDONLY << CONN_CTRL : 0));
	iad_desc.bInterfaceCount = 1;
	ac_hdr_desc.wTotalLength = cpu_to_le16(sizeof(ac_hdr_desc));

	if (EPIN_EN(opts)) {
		u16 len = le16_to_cpu(ac_hdr_desc.wTotalLength);

		len += sizeof(in_clk_src_desc);
		len += sizeof(usb_in_ot_desc);
		len += sizeof(io_in_it_desc);
		ac_hdr_desc.wTotalLength = cpu_to_le16(len);
		iad_desc.bInterfaceCount++;
	}
	if (EPOUT_EN(opts)) {
		u16 len = le16_to_cpu(ac_hdr_desc.wTotalLength);

		len += sizeof(out_clk_src_desc);
		len += sizeof(usb_out_it_desc);
		len += sizeof(io_out_ot_desc);
		ac_hdr_desc.wTotalLength = cpu_to_le16(len);
		iad_desc.bInterfaceCount++;
	}

	// ### SIPEED EDIT ###
	io_in_it_desc.wTerminalType = cpu_to_le16(opts->c_terminal_type);
	io_out_ot_desc.wTerminalType = cpu_to_le16(opts->p_terminal_type);
	// ### SIPEED EDIT END ###

	setup_headers(opts, fs_audio_desc, USB_SPEED_FULL);
	setup_headers(opts, hs_audio_desc, USB_SPEED_HIGH);
	setup_headers(opts, ss_audio_desc, USB_SPEED_SUPER);
}

static int afunc_validate_opts(struct g_audio *agdev, struct device *dev)
{
	struct f_uac2_opts *opts = g_audio_to_uac2_opts(agdev);

	if (opts->p_jack_detect && !EPIN_EN(opts)) {
		dev_err(dev, "Error: jack detection needs USB IN channels\n");
		return -EINVAL;
	}
	if (!opts->p_chmask && !opts->c_chmask) {
		dev_err(dev, "Error: no playback and capture channels\n");
		return -EINVAL;
	} else if (opts->p_chmask & ~UAC2_CHANNEL_MASK) {
		dev_err(dev, "Error: unsupported playback channels mask\n");
		return -EINVAL;
	} else if (opts->c_chmask & ~UAC2_CHANNEL_MASK) {
		dev_err(dev, "Error: unsupported capture channels mask\n");
		return -EINVAL;
	} else if ((opts->p_ssize < 1) || (opts->p_ssize > 4)) {
		dev_err(dev, "Error: incorrect playback sample size\n");
		return -EINVAL;
	} else if ((opts->c_ssize < 1) || (opts->c_ssize > 4)) {
		dev_err(dev, "Error: incorrect capture sample size\n");
		return -EINVAL;
	} else if (!opts->p_srate) {
		dev_err(dev, "Error: incorrect playback sampling rate\n");
		return -EINVAL;
	} else if (!opts->c_srate) {
		dev_err(dev, "Error: incorrect capture sampling rate\n");
		return -EINVAL;
	}

	return 0;
}
// ### SIPEED EDIT END ###

static int
afunc_bind(struct usb_configuration *cfg, struct usb_function *fn)
{
	struct f_uac2 *uac2 = func_to_uac2(fn);
	struct g_audio *agdev = func_to_g_audio(fn);
	struct usb_composite_dev *cdev = cfg->cdev;
	struct usb_gadget *gadget = cdev->gadget;
	struct device *dev = &gadget->dev;
	// ### SIPEED EDIT ###
	struct f_uac2_opts *uac2_opts = g_audio_to_uac2_opts(agdev);
	// ### SIPEED EDIT END ###
	struct usb_string *us;
	struct usb_ep *jack_ep = NULL;
	struct uac2_jack_notify *notify = NULL;
	int ret;

	// ### SIPEED EDIT ###
	ret = afunc_validate_opts(agdev, dev);
	if (ret)
		return ret;

	strings_fn[STR_ASSOC].s = uac2_opts->function_name;
	// ### SIPEED EDIT END ###

	us = usb_gstrings_attach(cdev, fn_strings, ARRAY_SIZE(strings_fn));
	if (IS_ERR(us))
		return PTR_ERR(us);
	iad_desc.iFunction = us[STR_ASSOC].id;
	std_ac_if_desc.iInterface = us[STR_IF_CTRL].id;
	in_clk_src_desc.iClockSource = us[STR_CLKSRC_IN].id;
	out_clk_src_desc.iClockSource = us[STR_CLKSRC_OUT].id;
	usb_out_it_desc.iTerminal = us[STR_USB_IT].id;
	io_in_it_desc.iTerminal = us[STR_IO_IT].id;
	usb_in_ot_desc.iTerminal = us[STR_USB_OT].id;
	io_out_ot_desc.iTerminal = us[STR_IO_OT].id;
	std_as_out_if0_desc.iInterface = us[STR_AS_OUT_ALT0].id;
	std_as_out_if1_desc.iInterface = us[STR_AS_OUT_ALT1].id;
	std_as_in_if0_desc.iInterface = us[STR_AS_IN_ALT0].id;
	std_as_in_if1_desc.iInterface = us[STR_AS_IN_ALT1].id;


	/* Initialize the configurable parameters */
	usb_out_it_desc.bNrChannels = num_channels(uac2_opts->c_chmask);
	usb_out_it_desc.bmChannelConfig = cpu_to_le32(uac2_opts->c_chmask);
	io_in_it_desc.bNrChannels = num_channels(uac2_opts->p_chmask);
	io_in_it_desc.bmChannelConfig = cpu_to_le32(uac2_opts->p_chmask);
	as_out_hdr_desc.bNrChannels = num_channels(uac2_opts->c_chmask);
	as_out_hdr_desc.bmChannelConfig = cpu_to_le32(uac2_opts->c_chmask);
	as_in_hdr_desc.bNrChannels = num_channels(uac2_opts->p_chmask);
	as_in_hdr_desc.bmChannelConfig = cpu_to_le32(uac2_opts->p_chmask);
	as_out_fmt1_desc.bSubslotSize = uac2_opts->c_ssize;
	as_out_fmt1_desc.bBitResolution = uac2_opts->c_ssize * 8;
	as_in_fmt1_desc.bSubslotSize = uac2_opts->p_ssize;
	as_in_fmt1_desc.bBitResolution = uac2_opts->p_ssize * 8;

	snprintf(clksrc_in, sizeof(clksrc_in), "%uHz", uac2_opts->p_srate);
	snprintf(clksrc_out, sizeof(clksrc_out), "%uHz", uac2_opts->c_srate);

	ret = usb_interface_id(cfg, fn);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}
	iad_desc.bFirstInterface = ret;

	std_ac_if_desc.bInterfaceNumber = ret;
	uac2->ac_intf = ret;
	uac2->ac_alt = 0;

	// ### SIPEED EDIT ###
	if (EPOUT_EN(uac2_opts)) {
		ret = usb_interface_id(cfg, fn);
		if (ret < 0) {
			dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
			return ret;
		}
		std_as_out_if0_desc.bInterfaceNumber = ret;
		std_as_out_if1_desc.bInterfaceNumber = ret;
		uac2->as_out_intf = ret;
		uac2->as_out_alt = 0;
	}

	if (EPIN_EN(uac2_opts)) {
		ret = usb_interface_id(cfg, fn);
		if (ret < 0) {
			dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
			return ret;
		}
		std_as_in_if0_desc.bInterfaceNumber = ret;
		std_as_in_if1_desc.bInterfaceNumber = ret;
		uac2->as_in_intf = ret;
		uac2->as_in_alt = 0;
	}

	/* Calculate wMaxPacketSize according to audio bandwidth */
	ret = set_ep_max_packet_size(uac2_opts, &fs_epin_desc, USB_SPEED_FULL,
				     true);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	ret = set_ep_max_packet_size(uac2_opts, &fs_epout_desc, USB_SPEED_FULL,
				     false);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	ret = set_ep_max_packet_size(uac2_opts, &hs_epin_desc, USB_SPEED_HIGH,
				     true);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	ret = set_ep_max_packet_size(uac2_opts, &hs_epout_desc, USB_SPEED_HIGH,
				     false);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	ret = set_ep_max_packet_size(uac2_opts, &ss_epin_desc, USB_SPEED_SUPER,
				     true);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	ret = set_ep_max_packet_size(uac2_opts, &ss_epout_desc, USB_SPEED_SUPER,
				     false);
	if (ret < 0) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return ret;
	}

	if (EPOUT_EN(uac2_opts)) {
		agdev->out_ep = usb_ep_autoconfig(gadget, &fs_epout_desc);
		if (!agdev->out_ep) {
			dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
			return -ENODEV;
		}
	}

	if (EPIN_EN(uac2_opts)) {
		agdev->in_ep = usb_ep_autoconfig(gadget, &fs_epin_desc);
		if (!agdev->in_ep) {
			dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
			return -ENODEV;
		}
	}

	agdev->in_ep_maxpsize = max_t(u16,
				le16_to_cpu(fs_epin_desc.wMaxPacketSize),
				le16_to_cpu(hs_epin_desc.wMaxPacketSize));
	agdev->out_ep_maxpsize = max_t(u16,
				le16_to_cpu(fs_epout_desc.wMaxPacketSize),
				le16_to_cpu(hs_epout_desc.wMaxPacketSize));

	agdev->in_ep_maxpsize = max_t(u16, agdev->in_ep_maxpsize,
				le16_to_cpu(ss_epin_desc.wMaxPacketSize));
	agdev->out_ep_maxpsize = max_t(u16, agdev->out_ep_maxpsize,
				le16_to_cpu(ss_epout_desc.wMaxPacketSize));

	hs_epout_desc.bEndpointAddress = fs_epout_desc.bEndpointAddress;
	hs_epin_desc.bEndpointAddress = fs_epin_desc.bEndpointAddress;
	ss_epout_desc.bEndpointAddress = fs_epout_desc.bEndpointAddress;
	ss_epin_desc.bEndpointAddress = fs_epin_desc.bEndpointAddress;

	/* Allocate last so existing audio endpoint selection stays unchanged. */
	if (uac2_opts->p_jack_detect) {
		fs_jack_ep_desc.bEndpointAddress = USB_DIR_IN;
		jack_ep = usb_ep_autoconfig(gadget, &fs_jack_ep_desc);
		if (!jack_ep)
			return -ENODEV;
		hs_jack_ep_desc.bEndpointAddress = fs_jack_ep_desc.bEndpointAddress;
		ss_jack_ep_desc.bEndpointAddress = fs_jack_ep_desc.bEndpointAddress;
	}
	setup_descriptor(uac2_opts);
	/* Never use the mutable global descriptor for run-time entity routing. */
	uac2->p_it_id = io_in_it_desc.bTerminalID;
	if (jack_ep) {
		notify = uac2_jack_alloc(jack_ep, uac2->p_it_id, uac2->ac_intf);
		if (!notify)
			return -ENOMEM;
	}

	ret = usb_assign_descriptors(fn, fs_audio_desc, hs_audio_desc, ss_audio_desc,
				     ss_audio_desc);
	// ### SIPEED EDIT END ###
	if (ret)
		goto err_free_notify;

	agdev->gadget = gadget;

	agdev->params.p_chmask = uac2_opts->p_chmask;
	agdev->params.p_srate = uac2_opts->p_srate;
	agdev->params.p_ssize = uac2_opts->p_ssize;
	agdev->params.c_chmask = uac2_opts->c_chmask;
	agdev->params.c_srate = uac2_opts->c_srate;
	agdev->params.c_ssize = uac2_opts->c_ssize;
	agdev->params.req_number = uac2_opts->req_number;
	ret = g_audio_setup(agdev, "UAC2 PCM", "UAC2_Gadget");
	if (ret)
		goto err_free_descs;
	mutex_lock(&uac2_opts->lock);
	uac2->jack_notify = notify;
	mutex_unlock(&uac2_opts->lock);
	return 0;

err_free_descs:
	usb_free_all_descriptors(fn);
	agdev->gadget = NULL;
err_free_notify:
	if (notify)
		kref_put(&notify->ref, uac2_jack_release);
	return ret;
}

static int
afunc_set_alt(struct usb_function *fn, unsigned intf, unsigned alt)
{
	struct usb_composite_dev *cdev = fn->config->cdev;
	struct f_uac2 *uac2 = func_to_uac2(fn);
	struct usb_gadget *gadget = cdev->gadget;
	struct device *dev = &gadget->dev;
	int ret = 0;

	/* No i/f has more than 2 alt settings */
	if (alt > 1) {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return -EINVAL;
	}

	if (intf == uac2->ac_intf) {
		/* Control I/f has only 1 AltSetting - 0 */
		if (alt) {
			dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
			return -EINVAL;
		}
		return uac2_jack_start(fn);
	}

	if (intf == uac2->as_out_intf) {
		uac2->as_out_alt = alt;

		if (alt)
			ret = u_audio_start_capture(&uac2->g_audio);
		else
			u_audio_stop_capture(&uac2->g_audio);
	} else if (intf == uac2->as_in_intf) {
		uac2->as_in_alt = alt;

		if (alt)
			ret = u_audio_start_playback(&uac2->g_audio);
		else
			u_audio_stop_playback(&uac2->g_audio);
	} else {
		dev_err(dev, "%s:%d Error!\n", __func__, __LINE__);
		return -EINVAL;
	}

	return ret;
}

static int
afunc_get_alt(struct usb_function *fn, unsigned intf)
{
	struct f_uac2 *uac2 = func_to_uac2(fn);
	struct g_audio *agdev = func_to_g_audio(fn);

	if (intf == uac2->ac_intf)
		return uac2->ac_alt;
	else if (intf == uac2->as_out_intf)
		return uac2->as_out_alt;
	else if (intf == uac2->as_in_intf)
		return uac2->as_in_alt;
	else
		dev_err(&agdev->gadget->dev,
			"%s:%d Invalid Interface %d!\n",
			__func__, __LINE__, intf);

	return -EINVAL;
}

static void
afunc_disable(struct usb_function *fn)
{
	struct f_uac2 *uac2 = func_to_uac2(fn);

	uac2->as_in_alt = 0;
	uac2->as_out_alt = 0;
	uac2_jack_stop(uac2->jack_notify);
	u_audio_stop_capture(&uac2->g_audio);
	u_audio_stop_playback(&uac2->g_audio);
}

static int
in_rq_cur(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	struct usb_request *req = fn->config->cdev->req;
	struct f_uac2 *uac2 = func_to_uac2(fn);
	struct g_audio *agdev = func_to_g_audio(fn);
	struct f_uac2_opts *opts;
	u16 w_length = le16_to_cpu(cr->wLength);
	u16 w_index = le16_to_cpu(cr->wIndex);
	u16 w_value = le16_to_cpu(cr->wValue);
	u8 entity_id = (w_index >> 8) & 0xff;
	u8 control_selector = w_value >> 8;
	int value = -EOPNOTSUPP;
	int p_srate, c_srate;

	/* Connector uses the terminal entity, not the clock-valid selector. */
	if (uac2->p_jack_detect && entity_id == uac2->p_it_id) {
		struct uac2_connectors_ctl_blk connector = { 0 };

		if (control_selector != UAC2_TE_CONNECTOR || (w_value & 0xff))
			return -EOPNOTSUPP;
		if (READ_ONCE(uac2->p_jack_connected)) {
			connector.bNrChannels = num_channels(uac2->p_chmask);
			connector.bmChannelConfig = cpu_to_le32(uac2->p_chmask);
		}
		value = min_t(unsigned int, w_length, sizeof(connector));
		memcpy(req->buf, &connector, value);
		return value;
	}

	opts = g_audio_to_uac2_opts(agdev);
	p_srate = opts->p_srate;
	c_srate = opts->c_srate;

	if (control_selector == UAC2_CS_CONTROL_SAM_FREQ) {
		struct cntrl_cur_lay3 c;
		memset(&c, 0, sizeof(struct cntrl_cur_lay3));

		if (entity_id == USB_IN_CLK_ID)
			c.dCUR = cpu_to_le32(p_srate);
		else if (entity_id == USB_OUT_CLK_ID)
			c.dCUR = cpu_to_le32(c_srate);

		value = min_t(unsigned, w_length, sizeof c);
		memcpy(req->buf, &c, value);
	} else if (control_selector == UAC2_CS_CONTROL_CLOCK_VALID) {
		*(u8 *)req->buf = 1;
		value = min_t(unsigned, w_length, 1);
	} else {
		dev_err(&agdev->gadget->dev,
			"%s:%d control_selector=%d TODO!\n",
			__func__, __LINE__, control_selector);
	}

	return value;
}

static int
in_rq_range(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	struct usb_request *req = fn->config->cdev->req;
	struct g_audio *agdev = func_to_g_audio(fn);
	struct f_uac2_opts *opts;
	u16 w_length = le16_to_cpu(cr->wLength);
	u16 w_index = le16_to_cpu(cr->wIndex);
	u16 w_value = le16_to_cpu(cr->wValue);
	u8 entity_id = (w_index >> 8) & 0xff;
	u8 control_selector = w_value >> 8;
	struct cntrl_range_lay3 r;
	int value = -EOPNOTSUPP;
	int p_srate, c_srate;

	opts = g_audio_to_uac2_opts(agdev);
	p_srate = opts->p_srate;
	c_srate = opts->c_srate;

	if (control_selector == UAC2_CS_CONTROL_SAM_FREQ) {
		if (entity_id == USB_IN_CLK_ID)
			r.dMIN = cpu_to_le32(p_srate);
		else if (entity_id == USB_OUT_CLK_ID)
			r.dMIN = cpu_to_le32(c_srate);
		else
			return -EOPNOTSUPP;

		r.dMAX = r.dMIN;
		r.dRES = 0;
		r.wNumSubRanges = cpu_to_le16(1);

		value = min_t(unsigned, w_length, sizeof r);
		memcpy(req->buf, &r, value);
	} else {
		dev_err(&agdev->gadget->dev,
			"%s:%d control_selector=%d TODO!\n",
			__func__, __LINE__, control_selector);
	}

	return value;
}

static int
ac_rq_in(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	if (cr->bRequest == UAC2_CS_CUR)
		return in_rq_cur(fn, cr);
	else if (cr->bRequest == UAC2_CS_RANGE)
		return in_rq_range(fn, cr);
	else
		return -EOPNOTSUPP;
}

static int
out_rq_cur(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	u16 w_length = le16_to_cpu(cr->wLength);
	u16 w_value = le16_to_cpu(cr->wValue);
	u8 control_selector = w_value >> 8;

	if (control_selector == UAC2_CS_CONTROL_SAM_FREQ)
		return w_length;

	return -EOPNOTSUPP;
}

static int
setup_rq_inf(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	struct f_uac2 *uac2 = func_to_uac2(fn);
	struct g_audio *agdev = func_to_g_audio(fn);
	u16 w_index = le16_to_cpu(cr->wIndex);
	u8 intf = w_index & 0xff;

	if (intf != uac2->ac_intf) {
		dev_err(&agdev->gadget->dev,
			"%s:%d Error!\n", __func__, __LINE__);
		return -EOPNOTSUPP;
	}

	if (cr->bRequestType & USB_DIR_IN)
		return ac_rq_in(fn, cr);
	else if (cr->bRequest == UAC2_CS_CUR)
		return out_rq_cur(fn, cr);

	return -EOPNOTSUPP;
}

static int
afunc_setup(struct usb_function *fn, const struct usb_ctrlrequest *cr)
{
	struct usb_composite_dev *cdev = fn->config->cdev;
	struct g_audio *agdev = func_to_g_audio(fn);
	struct usb_request *req = cdev->req;
	u16 w_length = le16_to_cpu(cr->wLength);
	int value = -EOPNOTSUPP;

	/* Only Class specific requests are supposed to reach here */
	if ((cr->bRequestType & USB_TYPE_MASK) != USB_TYPE_CLASS)
		return -EOPNOTSUPP;

	if ((cr->bRequestType & USB_RECIP_MASK) == USB_RECIP_INTERFACE)
		value = setup_rq_inf(fn, cr);
	else
		dev_err(&agdev->gadget->dev, "%s:%d Error!\n",
				__func__, __LINE__);

	if (value >= 0) {
		req->length = value;
		req->zero = value < w_length;
		value = usb_ep_queue(cdev->gadget->ep0, req, GFP_ATOMIC);
		if (value < 0) {
			dev_err(&agdev->gadget->dev,
				"%s:%d Error!\n", __func__, __LINE__);
			req->status = 0;
		}
	}

	return value;
}

static inline struct f_uac2_opts *to_f_uac2_opts(struct config_item *item)
{
	return container_of(to_config_group(item), struct f_uac2_opts,
			    func_inst.group);
}

static void f_uac2_attr_release(struct config_item *item)
{
	struct f_uac2_opts *opts = to_f_uac2_opts(item);

	usb_put_function_instance(&opts->func_inst);
}

static struct configfs_item_operations f_uac2_item_ops = {
	.release	= f_uac2_attr_release,
};

#define UAC2_ATTRIBUTE(name)						\
static ssize_t f_uac2_opts_##name##_show(struct config_item *item,	\
					 char *page)			\
{									\
	struct f_uac2_opts *opts = to_f_uac2_opts(item);		\
	int result;							\
									\
	mutex_lock(&opts->lock);					\
	result = sprintf(page, "%u\n", opts->name);			\
	mutex_unlock(&opts->lock);					\
									\
	return result;							\
}									\
									\
static ssize_t f_uac2_opts_##name##_store(struct config_item *item,	\
					  const char *page, size_t len)	\
{									\
	struct f_uac2_opts *opts = to_f_uac2_opts(item);		\
	int ret;							\
	u32 num;							\
									\
	mutex_lock(&opts->lock);					\
	if (opts->refcnt) {						\
		ret = -EBUSY;						\
		goto end;						\
	}								\
									\
	ret = kstrtou32(page, 0, &num);					\
	if (ret)							\
		goto end;						\
									\
	opts->name = num;						\
	ret = len;							\
									\
end:									\
	mutex_unlock(&opts->lock);					\
	return ret;							\
}									\
									\
CONFIGFS_ATTR(f_uac2_opts_, name)

// ### SIPEED EDIT ###
#define UAC2_ATTRIBUTE_STRING(name)					\
static ssize_t f_uac2_opts_##name##_show(struct config_item *item,	\
					 char *page)			\
{									\
	struct f_uac2_opts *opts = to_f_uac2_opts(item);		\
	int result;							\
									\
	mutex_lock(&opts->lock);					\
	result = scnprintf(page, sizeof(opts->name), "%s", opts->name);	\
	mutex_unlock(&opts->lock);					\
									\
	return result;							\
}									\
									\
static ssize_t f_uac2_opts_##name##_store(struct config_item *item,	\
					  const char *page, size_t len)	\
{									\
	struct f_uac2_opts *opts = to_f_uac2_opts(item);		\
	int ret = len;							\
									\
	mutex_lock(&opts->lock);					\
	if (opts->refcnt) {						\
		ret = -EBUSY;						\
		goto end;						\
	}								\
									\
	if (len && page[len - 1] == '\n')				\
		len--;							\
									\
	scnprintf(opts->name, min(sizeof(opts->name), len + 1),		\
		  "%s", page);						\
									\
end:									\
	mutex_unlock(&opts->lock);					\
	return ret;							\
}									\
									\
CONFIGFS_ATTR(f_uac2_opts_, name)
// ### SIPEED EDIT END ###

UAC2_ATTRIBUTE(p_chmask);
UAC2_ATTRIBUTE(p_srate);
UAC2_ATTRIBUTE(p_ssize);
UAC2_ATTRIBUTE(c_chmask);
UAC2_ATTRIBUTE(c_srate);
UAC2_ATTRIBUTE(c_ssize);
UAC2_ATTRIBUTE(req_number);
// ### SIPEED EDIT ###
UAC2_ATTRIBUTE_STRING(function_name);
UAC2_ATTRIBUTE(p_terminal_type);
UAC2_ATTRIBUTE(c_terminal_type);
// ### SIPEED EDIT END ###

/* Descriptor capability: configure before linking this function. */
static ssize_t f_uac2_opts_p_jack_detect_show(struct config_item *item,
					   char *page)
{
	struct f_uac2_opts *opts = to_f_uac2_opts(item);
	int ret;

	mutex_lock(&opts->lock);
	ret = sprintf(page, "%u\n", opts->p_jack_detect);
	mutex_unlock(&opts->lock);
	return ret;
}

static ssize_t f_uac2_opts_p_jack_detect_store(struct config_item *item,
					    const char *page, size_t len)
{
	struct f_uac2_opts *opts = to_f_uac2_opts(item);
	bool enabled;
	int ret;

	ret = kstrtobool(page, &enabled);
	if (ret)
		return ret;
	mutex_lock(&opts->lock);
	if (opts->refcnt) {
		ret = -EBUSY;
	} else {
		opts->p_jack_detect = enabled;
		ret = len;
	}
	mutex_unlock(&opts->lock);
	return ret;
}
CONFIGFS_ATTR(f_uac2_opts_, p_jack_detect);

static ssize_t f_uac2_opts_p_jack_connected_show(struct config_item *item,
					      char *page)
{
	struct f_uac2_opts *opts = to_f_uac2_opts(item);
	int ret;

	mutex_lock(&opts->lock);
	ret = sprintf(page, "%u\n", opts->p_jack_connected);
	mutex_unlock(&opts->lock);
	return ret;
}

static ssize_t f_uac2_opts_p_jack_connected_store(struct config_item *item,
					       const char *page, size_t len)
{
	struct f_uac2_opts *opts = to_f_uac2_opts(item);
	struct f_uac2 *uac2;
	bool connected;
	int ret;

	ret = kstrtobool(page, &connected);
	if (ret)
		return ret;
	mutex_lock(&opts->lock);
	if (!opts->p_jack_detect) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	opts->p_jack_connected = connected;
	list_for_each_entry(uac2, &opts->jack_functions, jack_node)
		uac2_jack_set_connected(uac2, connected);
	/* The accepted state is authoritative; notification errors are logged. */
	ret = len;
out:
	mutex_unlock(&opts->lock);
	return ret;
}
CONFIGFS_ATTR(f_uac2_opts_, p_jack_connected);

static struct configfs_attribute *f_uac2_attrs[] = {
	&f_uac2_opts_attr_p_jack_detect,
	&f_uac2_opts_attr_p_jack_connected,
	&f_uac2_opts_attr_p_chmask,
	&f_uac2_opts_attr_p_srate,
	&f_uac2_opts_attr_p_ssize,
	&f_uac2_opts_attr_c_chmask,
	&f_uac2_opts_attr_c_srate,
	&f_uac2_opts_attr_c_ssize,
	&f_uac2_opts_attr_req_number,
	// ### SIPEED EDIT ###
	&f_uac2_opts_attr_function_name,
	&f_uac2_opts_attr_p_terminal_type,
	&f_uac2_opts_attr_c_terminal_type,
	// ### SIPEED EDIT END ###
	NULL,
};

static const struct config_item_type f_uac2_func_type = {
	.ct_item_ops	= &f_uac2_item_ops,
	.ct_attrs	= f_uac2_attrs,
	.ct_owner	= THIS_MODULE,
};

static void afunc_free_inst(struct usb_function_instance *f)
{
	struct f_uac2_opts *opts;

	opts = container_of(f, struct f_uac2_opts, func_inst);
	kfree(opts);
}

static struct usb_function_instance *afunc_alloc_inst(void)
{
	struct f_uac2_opts *opts;

	opts = kzalloc(sizeof(*opts), GFP_KERNEL);
	if (!opts)
		return ERR_PTR(-ENOMEM);

	mutex_init(&opts->lock);
	INIT_LIST_HEAD(&opts->jack_functions);
	opts->p_jack_detect = false;
	opts->p_jack_connected = true;
	opts->func_inst.free_func_inst = afunc_free_inst;

	config_group_init_type_name(&opts->func_inst.group, "",
				    &f_uac2_func_type);

	opts->p_chmask = UAC2_DEF_PCHMASK;
	opts->p_srate = UAC2_DEF_PSRATE;
	opts->p_ssize = UAC2_DEF_PSSIZE;
	opts->c_chmask = UAC2_DEF_CCHMASK;
	opts->c_srate = UAC2_DEF_CSRATE;
	opts->c_ssize = UAC2_DEF_CSSIZE;
	opts->req_number = UAC2_DEF_REQ_NUM;
	// ### SIPEED EDIT ###
	snprintf(opts->function_name, sizeof(opts->function_name), "Source/Sink");

	opts->p_terminal_type = UAC2_DEF_P_TERM_TYPE;
	opts->c_terminal_type = UAC2_DEF_C_TERM_TYPE;
	// ### SIPEED EDIT END ###

	return &opts->func_inst;
}

static void afunc_free(struct usb_function *f)
{
	struct g_audio *agdev;
	struct f_uac2_opts *opts;

	agdev = func_to_g_audio(f);
	opts = container_of(f->fi, struct f_uac2_opts, func_inst);
	mutex_lock(&opts->lock);
	list_del(&func_to_uac2(f)->jack_node);
	--opts->refcnt;
	mutex_unlock(&opts->lock);
	kfree(agdev);
}

static void afunc_unbind(struct usb_configuration *c, struct usb_function *f)
{
	struct g_audio *agdev = func_to_g_audio(f);
	struct f_uac2 *uac2 = func_to_uac2(f);
	struct f_uac2_opts *opts = g_audio_to_uac2_opts(agdev);
	struct uac2_jack_notify *notify;

	mutex_lock(&opts->lock);
	notify = uac2->jack_notify;
	uac2->jack_notify = NULL;
	mutex_unlock(&opts->lock);
	if (notify) {
		uac2_jack_stop(notify);
		/* A failed UDC disable leaves any queued request holding a ref. */
		kref_put(&notify->ref, uac2_jack_release);
	}
	g_audio_cleanup(agdev);
	usb_free_all_descriptors(f);

	agdev->gadget = NULL;
}

static struct usb_function *afunc_alloc(struct usb_function_instance *fi)
{
	struct f_uac2	*uac2;
	struct f_uac2_opts *opts;

	uac2 = kzalloc(sizeof(*uac2), GFP_KERNEL);
	if (uac2 == NULL)
		return ERR_PTR(-ENOMEM);

	opts = container_of(fi, struct f_uac2_opts, func_inst);
	mutex_lock(&opts->lock);
	uac2->p_jack_detect = opts->p_jack_detect;
	uac2->p_jack_connected = opts->p_jack_connected;
	uac2->p_chmask = opts->p_chmask;
	list_add_tail(&uac2->jack_node, &opts->jack_functions);
	++opts->refcnt;
	mutex_unlock(&opts->lock);

	uac2->g_audio.func.name = "uac2_func";
	uac2->g_audio.func.bind = afunc_bind;
	uac2->g_audio.func.unbind = afunc_unbind;
	uac2->g_audio.func.set_alt = afunc_set_alt;
	uac2->g_audio.func.get_alt = afunc_get_alt;
	uac2->g_audio.func.disable = afunc_disable;
	uac2->g_audio.func.setup = afunc_setup;
	uac2->g_audio.func.free_func = afunc_free;

	return &uac2->g_audio.func;
}

DECLARE_USB_FUNCTION_INIT(uac2, afunc_alloc_inst, afunc_alloc);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Yadwinder Singh");
MODULE_AUTHOR("Jaswinder Singh");
