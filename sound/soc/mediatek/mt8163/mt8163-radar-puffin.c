// SPDX-License-Identifier: GPL-2.0-only
/*
 * Amazon Radar-Puffin MT8163 sound card.
 *
 * Keep the three board links that are present in the release product:
 * external DAC playback, FPGA SPI microphone capture, and the raw I2S1
 * playback endpoint.  Link IDs intentionally retain the stock PCM device
 * numbers without registering the unused voice/FM/HDMI links.
 */

#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>

#include <sound/pcm_params.h>
#include <sound/soc.h>

#include "mt8163-afe.h"
#include "mt8163-mclk.h"

#define RADAR_CARD_NAME			"mt-snd-card"
#define RADAR_AFE_COMPATIBLE		"mediatek,mt8163-soc-pcm-dl1"
#define RADAR_SPI_COMPATIBLE		"amzn-mtk,spi-audio-pltfm"
#define RADAR_DAC_COMPATIBLE		"ti,tlv320aic32x4"
#define RADAR_ADC_COMPATIBLE		"ti,tlv320aic3101"

#define RADAR_DL1_DAI			"mt-soc-dl1dai-driver"
#define RADAR_SPI_DAI			"amzn-mt-spi-pcm"
#define RADAR_DAC_DAI			"tlv320aic32x4-hifi"
#define RADAR_ADC_DAI			"tlv320aic3101-codec"

#define RADAR_MCLK_RATE			9600000U

#define RADAR_ADC_PLL_BCLK		1
#define RADAR_ADC_PLL_MCLK		2

/* TLV320AIC32x4 page-zero registers used by stock board controls. */
#define RADAR_DAC_DOUTCTL		53
#define RADAR_DAC_PROCESSING_BLOCK	60
#define RADAR_DAC_SETUP			63
#define RADAR_DRC_CTRL1			68
#define RADAR_PUFFIN_DAC_PROCESSING_BLOCK 2
#define RADAR_DRC_DISABLED		0x0f
#define RADAR_DAC_HEADSTART		RADAR_AIC32X4_REG(1, 20)
#define RADAR_HP_AMP_SOFT_STARTUP	0x1d
/*
 * HP analog output driver gain/mute registers.  Bit 6 is the analog mute:
 * the codec resets with HPLGAIN/HPRGAIN=0x40 (muted).  The 3.18 codec
 * driver unmutes them in hw_params; the 6.1 driver never does, so the HP
 * drivers stay hardware-muted and no signal reaches the speaker even when
 * the entire digital path is correct.
 */
#define RADAR_HPLGAIN			RADAR_AIC32X4_REG(1, 18)
#define RADAR_HPRGAIN			RADAR_AIC32X4_REG(1, 19)
#define RADAR_HP_DRIVER_MUTE		BIT(6)
/*
 * DAC digital volume registers.  Bit 7 is a per-channel digital mute that
 * is independent of DACMUTE (reg 64) and the HP driver mute (HPLGAIN/
 * HPRGAIN bit 6).  The "PCM Playback Volume" mixer control only writes
 * bits 0-6; bit 7 must be cleared explicitly.  The 3.18 live dump shows
 * DACVOL=0x00 (not muted); the 6.1 codec leaves bit 7 set, silencing the
 * DAC digital output regardless of the volume setting.
 */
#define RADAR_LDACVOL			RADAR_AIC32X4_REG(0, 65)
#define RADAR_RDACVOL			RADAR_AIC32X4_REG(0, 66)
#define RADAR_DACVOL_MUTE		BIT(7)
/*
 * Proven Linux 3.18 PLL/divider values for 9.6 MHz MCLK → 48 kHz S16 stereo.
 * The 6.1 CCF clock solver computes P=1,J=10,D=2400,NDAC=8,MDAC=2,BCLK=4
 * which leaves DACFLAG2=0x44 (right DAC only).  The 3.18 hardcoded table
 * uses P=5,J=48,D=0,NDAC=5,MDAC=3,BCLK=1 giving DACFLAG2=0x88 (both DACs).
 */
#define RADAR_PLLPR			RADAR_AIC32X4_REG(0, 5)
#define RADAR_PLLJ			RADAR_AIC32X4_REG(0, 6)
#define RADAR_PLLD_MSB			RADAR_AIC32X4_REG(0, 7)
#define RADAR_PLLD_LSB			RADAR_AIC32X4_REG(0, 8)
#define RADAR_NDAC			RADAR_AIC32X4_REG(0, 11)
#define RADAR_MDAC			RADAR_AIC32X4_REG(0, 12)
#define RADAR_DOSR_MSB			RADAR_AIC32X4_REG(0, 13)
#define RADAR_DOSR_LSB			RADAR_AIC32X4_REG(0, 14)
#define RADAR_BCLKN			RADAR_AIC32X4_REG(0, 30)
#define RADAR_DACSETUP			RADAR_AIC32X4_REG(0, 63)
#define RADAR_PLLPR_318			0xd1	/* P=5, R=1, PLL enabled */
#define RADAR_PLLJ_318			0x30	/* J=48 */
#define RADAR_NDAC_318			0x85	/* enabled, div=5 */
#define RADAR_MDAC_318			0x83	/* enabled, div=3 */
#define RADAR_DOSR_318			128
#define RADAR_BCLKN_318			0x81	/* enabled, div=1 */
#define RADAR_DAC_SOFT_STEP		0x02
#define RADAR_DAC_MFP2_MASK		(BIT(2) | BIT(0))
#define RADAR_DAC_MFP2_GPIO		BIT(2)
#define RADAR_DAC_LEFT_TO_LEFT		BIT(4)
#define RADAR_DAC_RIGHT_TO_RIGHT	BIT(2)
#define RADAR_DAC_ROUTE_MASK		GENMASK(5, 2)
#define RADAR_AIC32X4_REG(_page, _reg)	(((_page) * 128) + (_reg))
#define RADAR_PUFFIN_PROFILE(_page, _reg, _value)	\
	{ .reg = RADAR_AIC32X4_REG(_page, _reg), .def = (_value) }

/*
 * Stock radar_puffin ext_speaker_output profile recovered from the final
 * Linux 3.18 audio_device.xml path.  The engine duplicates its mono speaker
 * bus into both I2S channels; these coefficients then high-pass HPL for the
 * tweeter and low-pass HPR for the larger cone, with the stock DRC settings.
 */
static const struct reg_sequence radar_puffin_ext_speaker_profile[] = {
	RADAR_PUFFIN_PROFILE(44, 12, 122),
	RADAR_PUFFIN_PROFILE(44, 13, 248),
	RADAR_PUFFIN_PROFILE(44, 14, 206),
	RADAR_PUFFIN_PROFILE(44, 16, 133),
	RADAR_PUFFIN_PROFILE(44, 17, 7),
	RADAR_PUFFIN_PROFILE(44, 18, 50),
	RADAR_PUFFIN_PROFILE(44, 20, 122),
	RADAR_PUFFIN_PROFILE(44, 21, 248),
	RADAR_PUFFIN_PROFILE(44, 22, 206),
	RADAR_PUFFIN_PROFILE(44, 24, 83),
	RADAR_PUFFIN_PROFILE(44, 25, 31),
	RADAR_PUFFIN_PROFILE(44, 26, 201),
	RADAR_PUFFIN_PROFILE(44, 28, 202),
	RADAR_PUFFIN_PROFILE(44, 29, 4),
	RADAR_PUFFIN_PROFILE(44, 30, 191),
	RADAR_PUFFIN_PROFILE(44, 32, 87),
	RADAR_PUFFIN_PROFILE(44, 33, 14),
	RADAR_PUFFIN_PROFILE(44, 34, 180),
	RADAR_PUFFIN_PROFILE(44, 36, 168),
	RADAR_PUFFIN_PROFILE(44, 37, 241),
	RADAR_PUFFIN_PROFILE(44, 38, 76),
	RADAR_PUFFIN_PROFILE(44, 40, 87),
	RADAR_PUFFIN_PROFILE(44, 41, 14),
	RADAR_PUFFIN_PROFILE(44, 42, 180),
	RADAR_PUFFIN_PROFILE(44, 44, 83),
	RADAR_PUFFIN_PROFILE(44, 45, 31),
	RADAR_PUFFIN_PROFILE(44, 46, 201),
	RADAR_PUFFIN_PROFILE(44, 48, 202),
	RADAR_PUFFIN_PROFILE(44, 49, 4),
	RADAR_PUFFIN_PROFILE(44, 50, 191),
	RADAR_PUFFIN_PROFILE(44, 52, 127),
	RADAR_PUFFIN_PROFILE(44, 53, 255),
	RADAR_PUFFIN_PROFILE(44, 54, 255),
	RADAR_PUFFIN_PROFILE(44, 56, 128),
	RADAR_PUFFIN_PROFILE(44, 57, 49),
	RADAR_PUFFIN_PROFILE(44, 58, 110),
	RADAR_PUFFIN_PROFILE(44, 60, 127),
	RADAR_PUFFIN_PROFILE(44, 61, 162),
	RADAR_PUFFIN_PROFILE(44, 62, 193),
	RADAR_PUFFIN_PROFILE(44, 64, 127),
	RADAR_PUFFIN_PROFILE(44, 65, 206),
	RADAR_PUFFIN_PROFILE(44, 66, 146),
	RADAR_PUFFIN_PROFILE(44, 68, 128),
	RADAR_PUFFIN_PROFILE(44, 69, 93),
	RADAR_PUFFIN_PROFILE(44, 70, 63),
	RADAR_PUFFIN_PROFILE(45, 20, 127),
	RADAR_PUFFIN_PROFILE(45, 21, 116),
	RADAR_PUFFIN_PROFILE(45, 22, 152),
	RADAR_PUFFIN_PROFILE(45, 24, 128),
	RADAR_PUFFIN_PROFILE(45, 25, 139),
	RADAR_PUFFIN_PROFILE(45, 26, 104),
	RADAR_PUFFIN_PROFILE(45, 28, 127),
	RADAR_PUFFIN_PROFILE(45, 29, 116),
	RADAR_PUFFIN_PROFILE(45, 30, 152),
	RADAR_PUFFIN_PROFILE(45, 32, 127),
	RADAR_PUFFIN_PROFILE(45, 33, 116),
	RADAR_PUFFIN_PROFILE(45, 34, 1),
	RADAR_PUFFIN_PROFILE(45, 36, 129),
	RADAR_PUFFIN_PROFILE(45, 37, 21),
	RADAR_PUFFIN_PROFILE(45, 38, 161),
	RADAR_PUFFIN_PROFILE(45, 40, 3),
	RADAR_PUFFIN_PROFILE(45, 41, 238),
	RADAR_PUFFIN_PROFILE(45, 42, 235),
	RADAR_PUFFIN_PROFILE(45, 44, 3),
	RADAR_PUFFIN_PROFILE(45, 45, 238),
	RADAR_PUFFIN_PROFILE(45, 46, 235),
	RADAR_PUFFIN_PROFILE(45, 48, 3),
	RADAR_PUFFIN_PROFILE(45, 49, 238),
	RADAR_PUFFIN_PROFILE(45, 50, 235),
	RADAR_PUFFIN_PROFILE(45, 52, 83),
	RADAR_PUFFIN_PROFILE(45, 53, 31),
	RADAR_PUFFIN_PROFILE(45, 54, 201),
	RADAR_PUFFIN_PROFILE(45, 56, 202),
	RADAR_PUFFIN_PROFILE(45, 57, 4),
	RADAR_PUFFIN_PROFILE(45, 58, 191),
	RADAR_PUFFIN_PROFILE(45, 60, 3),
	RADAR_PUFFIN_PROFILE(45, 61, 238),
	RADAR_PUFFIN_PROFILE(45, 62, 235),
	RADAR_PUFFIN_PROFILE(45, 64, 3),
	RADAR_PUFFIN_PROFILE(45, 65, 238),
	RADAR_PUFFIN_PROFILE(45, 66, 235),
	RADAR_PUFFIN_PROFILE(45, 68, 3),
	RADAR_PUFFIN_PROFILE(45, 69, 238),
	RADAR_PUFFIN_PROFILE(45, 70, 235),
	RADAR_PUFFIN_PROFILE(45, 72, 83),
	RADAR_PUFFIN_PROFILE(45, 73, 31),
	RADAR_PUFFIN_PROFILE(45, 74, 201),
	RADAR_PUFFIN_PROFILE(45, 76, 202),
	RADAR_PUFFIN_PROFILE(45, 77, 4),
	RADAR_PUFFIN_PROFILE(45, 78, 191),
	RADAR_PUFFIN_PROFILE(46, 52, 127),
	RADAR_PUFFIN_PROFILE(46, 53, 255),
	RADAR_PUFFIN_PROFILE(46, 54, 255),
	RADAR_PUFFIN_PROFILE(46, 55, 0),
	RADAR_PUFFIN_PROFILE(46, 56, 0),
	RADAR_PUFFIN_PROFILE(46, 57, 0),
	RADAR_PUFFIN_PROFILE(46, 58, 0),
	RADAR_PUFFIN_PROFILE(46, 59, 0),
	RADAR_PUFFIN_PROFILE(46, 60, 0),
	RADAR_PUFFIN_PROFILE(46, 61, 0),
	RADAR_PUFFIN_PROFILE(46, 62, 0),
	RADAR_PUFFIN_PROFILE(46, 63, 0),
	RADAR_PUFFIN_PROFILE(46, 64, 127),
	RADAR_PUFFIN_PROFILE(46, 65, 255),
	RADAR_PUFFIN_PROFILE(46, 66, 255),
	RADAR_PUFFIN_PROFILE(46, 67, 0),
	RADAR_PUFFIN_PROFILE(46, 68, 0),
	RADAR_PUFFIN_PROFILE(46, 69, 0),
	RADAR_PUFFIN_PROFILE(46, 70, 0),
	RADAR_PUFFIN_PROFILE(46, 71, 0),
	RADAR_PUFFIN_PROFILE(46, 72, 0),
	RADAR_PUFFIN_PROFILE(46, 73, 0),
	RADAR_PUFFIN_PROFILE(46, 74, 0),
	RADAR_PUFFIN_PROFILE(46, 75, 0),
	RADAR_PUFFIN_PROFILE(0, 68, 7),
	RADAR_PUFFIN_PROFILE(0, 69, 0),
	RADAR_PUFFIN_PROFILE(0, 70, 198),
};

enum radar_link_id {
	RADAR_LINK_DAC,
	RADAR_LINK_CAPTURE,
	RADAR_LINK_I2S1,
	RADAR_LINK_NUM,
};

struct radar_card {
	struct snd_soc_card card;
	struct snd_soc_dai_link links[RADAR_LINK_NUM];
	struct snd_soc_dai_link_component cpus[RADAR_LINK_NUM];
	struct snd_soc_dai_link_component platforms[RADAR_LINK_NUM];
	struct snd_soc_dai_link_component codecs[RADAR_LINK_NUM];
	struct device_node *afe_np;
	struct device_node *spi_np;
	struct device_node *dac_np;
	struct device_node *adc_np;
	struct platform_device *afe_pdev;
	struct mt8163_mclk *mclk;
	struct snd_soc_pcm_runtime *speaker_rtd;
	unsigned int low_jitter;
	unsigned int channel_config;
	unsigned int linein_adc;
	unsigned int amp_fault_enable;
	unsigned int codec_mute;
	unsigned int amp_enable;
	unsigned int dac_route;
	unsigned int right_only;
	unsigned int hd_output;
	unsigned int ignore_ramp;
	bool speaker_mclk_enabled;
};

static const char * const radar_on_off[] = { "Off", "On" };
static SOC_ENUM_SINGLE_EXT_DECL(radar_on_off_enum, radar_on_off);

static const char * const radar_channel_config[] = {
	"Stereo", "MonoLeft", "MonoRight"
};
static SOC_ENUM_SINGLE_EXT_DECL(radar_channel_enum, radar_channel_config);

static struct radar_card *radar_kcontrol_priv(struct snd_kcontrol *kcontrol)
{
	struct snd_soc_card *card = snd_kcontrol_chip(kcontrol);

	return snd_soc_card_get_drvdata(card);
}

static int radar_low_jitter_get(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->low_jitter;
	return 0;
}

static int radar_low_jitter_put(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];

	if (requested >= ARRAY_SIZE(radar_on_off))
		return -EINVAL;
	if (priv->low_jitter == requested)
		return 0;
	priv->low_jitter = requested;
	return 1;
}

static int radar_channel_get(struct snd_kcontrol *kcontrol,
			     struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->channel_config;
	return 0;
}

static int radar_channel_put(struct snd_kcontrol *kcontrol,
			     struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];

	if (requested >= ARRAY_SIZE(radar_channel_config))
		return -EINVAL;
	if (priv->channel_config == requested)
		return 0;
	priv->channel_config = requested;
	return 1;
}

#define RADAR_STATE_CONTROL_FUNCS(_name, _field)				\
static int radar_##_name##_get(struct snd_kcontrol *kcontrol,		\
			       struct snd_ctl_elem_value *value)		\
{									\
	value->value.enumerated.item[0] =					\
		radar_kcontrol_priv(kcontrol)->_field;			\
	return 0;								\
}									\
static int radar_##_name##_put(struct snd_kcontrol *kcontrol,		\
			       struct snd_ctl_elem_value *value)		\
{									\
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);		\
	unsigned int requested = value->value.enumerated.item[0];		\
	if (requested >= ARRAY_SIZE(radar_on_off))				\
		return -EINVAL;							\
	if (priv->_field == requested)					\
		return 0;							\
	priv->_field = requested;						\
	return 1;								\
}

RADAR_STATE_CONTROL_FUNCS(linein, linein_adc)
RADAR_STATE_CONTROL_FUNCS(amp_fault, amp_fault_enable)
RADAR_STATE_CONTROL_FUNCS(hd_output, hd_output)
RADAR_STATE_CONTROL_FUNCS(ignore_ramp, ignore_ramp)

static int radar_apply_dac_route(struct radar_card *priv)
{
	struct snd_soc_component *component;
	unsigned int route;

	if (!priv->speaker_rtd)
		return -ENODEV;
	component = asoc_rtd_to_codec(priv->speaker_rtd, 0)->component;
	route = RADAR_DAC_RIGHT_TO_RIGHT;
	if (!priv->right_only)
		route |= RADAR_DAC_LEFT_TO_LEFT;
	return snd_soc_component_update_bits(component, RADAR_DAC_SETUP,
					     RADAR_DAC_ROUTE_MASK, route);
}

static int radar_right_only_get(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->right_only;
	return 0;
}

static int radar_right_only_put(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];
	int ret;

	if (requested >= ARRAY_SIZE(radar_on_off))
		return -EINVAL;
	if (priv->right_only == requested)
		return 0;
	priv->right_only = requested;
	ret = radar_apply_dac_route(priv);
	if (ret) {
		priv->right_only = !requested;
		return ret;
	}
	return 1;
}

static int radar_amp_get(struct snd_kcontrol *kcontrol,
			 struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->amp_enable;
	return 0;
}

static int radar_amp_put(struct snd_kcontrol *kcontrol,
			 struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];
	int ret;

	if (requested >= ARRAY_SIZE(radar_on_off))
		return -EINVAL;
	if (priv->amp_enable == requested)
		return 0;
	ret = mt8163_afe_select_amp(&priv->afe_pdev->dev, requested);
	if (ret)
		return ret;
	priv->amp_enable = requested;
	return 1;
}

static int radar_dac_route_get(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->dac_route;
	return 0;
}

static int radar_dac_route_put(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];
	int ret;

	if (requested >= ARRAY_SIZE(radar_on_off))
		return -EINVAL;
	if (priv->dac_route == requested)
		return 0;
	ret = mt8163_afe_select_dac(&priv->afe_pdev->dev, requested);
	if (ret)
		return ret;
	priv->dac_route = requested;
	return 1;
}

static int radar_codec_mute_get(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	value->value.enumerated.item[0] =
		radar_kcontrol_priv(kcontrol)->codec_mute;
	return 0;
}

static int radar_codec_mute_put(struct snd_kcontrol *kcontrol,
				struct snd_ctl_elem_value *value)
{
	struct radar_card *priv = radar_kcontrol_priv(kcontrol);
	unsigned int requested = value->value.enumerated.item[0];
	struct snd_soc_dai *codec_dai;
	int ret;

	if (requested >= ARRAY_SIZE(radar_on_off))
		return -EINVAL;
	if (!priv->speaker_rtd)
		return -ENODEV;
	if (priv->codec_mute == requested)
		return 0;

	codec_dai = asoc_rtd_to_codec(priv->speaker_rtd, 0);
	ret = snd_soc_component_update_bits(codec_dai->component,
					   RADAR_DAC_DOUTCTL,
					   RADAR_DAC_MFP2_MASK,
					   RADAR_DAC_MFP2_GPIO |
					   (requested ? BIT(0) : 0));
	if (ret < 0)
		return ret;
	priv->codec_mute = requested;
	return 1;
}

static const struct snd_kcontrol_new radar_controls[] = {
	SOC_ENUM_EXT("I2S low Jitter function", radar_on_off_enum,
		     radar_low_jitter_get, radar_low_jitter_put),
	SOC_ENUM_EXT("Audio_I2S0dl1_hd_Switch", radar_on_off_enum,
		     radar_hd_output_get, radar_hd_output_put),
	SOC_ENUM_EXT("Board Channel Config", radar_channel_enum,
		     radar_channel_get, radar_channel_put),
	SOC_ENUM_EXT("LineIn ADC", radar_on_off_enum,
		     radar_linein_get, radar_linein_put),
	SOC_ENUM_EXT("Amp Fault Enable", radar_on_off_enum,
		     radar_amp_fault_get, radar_amp_fault_put),
	SOC_ENUM_EXT("MFP Gpio Mute", radar_on_off_enum,
		     radar_codec_mute_get, radar_codec_mute_put),
	SOC_ENUM_EXT("Ext_Speaker_Amp_Switch", radar_on_off_enum,
		     radar_amp_get, radar_amp_put),
	SOC_ENUM_EXT("Audio_DacMux_Setting", radar_on_off_enum,
		     radar_dac_route_get, radar_dac_route_put),
	SOC_ENUM_EXT("Right Channel Only", radar_on_off_enum,
		     radar_right_only_get, radar_right_only_put),
	SOC_ENUM_EXT("Ignore Ramp Up", radar_on_off_enum,
		     radar_ignore_ramp_get, radar_ignore_ramp_put),
};

static int radar_speaker_init(struct snd_soc_pcm_runtime *rtd)
{
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	struct snd_soc_dai *codec_dai = asoc_rtd_to_codec(rtd, 0);
	int ret;

	priv->speaker_rtd = rtd;
	priv->codec_mute = 1;
	ret = snd_soc_dai_digital_mute(codec_dai, 1,
				       SNDRV_PCM_STREAM_PLAYBACK);
	if (ret && ret != -ENOTSUPP)
		return ret;
	ret = snd_soc_component_update_bits(codec_dai->component,
					   RADAR_DAC_DOUTCTL,
					   RADAR_DAC_MFP2_MASK,
					   RADAR_DAC_MFP2_GPIO | BIT(0));
	if (ret < 0)
		return ret;
	ret = mt8163_afe_select_amp(&priv->afe_pdev->dev, false);
	if (ret)
		return ret;
	ret = mt8163_afe_select_dac(&priv->afe_pdev->dev, false);
	if (ret)
		return ret;
	return mt8163_afe_select_i2s(&priv->afe_pdev->dev, false);
}

static void radar_record_error(int *first, int ret)
{
	if (ret && !*first)
		*first = ret;
}

static int radar_speaker_enable_clocks(struct radar_card *priv)
{
	int idle_ret;
	int ret;

	ret = mt8163_afe_select_i2s(&priv->afe_pdev->dev, true);
	if (ret)
		return ret;
	ret = mt8163_afe_select_mclk(&priv->afe_pdev->dev);
	if (ret)
		goto idle_i2s;
	if (!priv->speaker_mclk_enabled) {
		ret = mt8163_mclk_enable(priv->mclk);
		if (ret)
			goto idle_i2s;
		priv->speaker_mclk_enabled = true;
	}
	return 0;

idle_i2s:
	idle_ret = mt8163_afe_select_i2s(&priv->afe_pdev->dev, false);
	if (idle_ret)
		dev_err(priv->card.dev,
			"Speaker clock rollback failed: primary=%d cleanup=%d\n",
			ret, idle_ret);
	return ret ? ret : idle_ret;
}

static int radar_speaker_safe(struct radar_card *priv)
{
	struct snd_soc_dai *codec_dai =
		asoc_rtd_to_codec(priv->speaker_rtd, 0);
	int first = 0;
	int ret;

	ret = snd_soc_dai_digital_mute(codec_dai, 1,
				       SNDRV_PCM_STREAM_PLAYBACK);
	if (ret == -ENOTSUPP)
		ret = 0;
	radar_record_error(&first, ret);
	/* Re-mute the HP analog output drivers to prevent pops and restore
	 * the codec's reset state (bit 6 set on HPLGAIN/HPRGAIN). */
	ret = snd_soc_component_update_bits(codec_dai->component,
					    RADAR_HPLGAIN,
					    RADAR_HP_DRIVER_MUTE,
					    RADAR_HP_DRIVER_MUTE);
	radar_record_error(&first, ret);
	ret = snd_soc_component_update_bits(codec_dai->component,
					    RADAR_HPRGAIN,
					    RADAR_HP_DRIVER_MUTE,
					    RADAR_HP_DRIVER_MUTE);
	radar_record_error(&first, ret);
	/* Re-mute the DAC digital volume on shutdown (0x80 = muted). */
	ret = snd_soc_component_write(codec_dai->component,
				      RADAR_LDACVOL, RADAR_DACVOL_MUTE);
	radar_record_error(&first, ret);
	ret = snd_soc_component_write(codec_dai->component,
				      RADAR_RDACVOL, RADAR_DACVOL_MUTE);
	radar_record_error(&first, ret);
	ret = snd_soc_component_update_bits(codec_dai->component,
					   RADAR_DAC_DOUTCTL,
					   RADAR_DAC_MFP2_MASK,
					   RADAR_DAC_MFP2_GPIO | BIT(0));
	radar_record_error(&first, ret);
	if (!ret)
		priv->codec_mute = 1;
	ret = mt8163_afe_select_amp(&priv->afe_pdev->dev, false);
	radar_record_error(&first, ret);
	if (!ret)
		priv->amp_enable = 0;
	ret = mt8163_afe_select_dac(&priv->afe_pdev->dev, false);
	radar_record_error(&first, ret);
	if (!ret)
		priv->dac_route = 0;
	if (priv->speaker_mclk_enabled) {
		mt8163_mclk_disable(priv->mclk);
		priv->speaker_mclk_enabled = false;
	}
	ret = mt8163_afe_select_i2s(&priv->afe_pdev->dev, false);
	radar_record_error(&first, ret);
	return first;
}

static int radar_speaker_fail_safe(struct radar_card *priv, int primary,
				   const char *operation)
{
	int safe_ret;

	safe_ret = radar_speaker_safe(priv);
	if (safe_ret)
		dev_err(priv->card.dev,
			"Speaker %s rollback failed: primary=%d cleanup=%d\n",
			operation, primary, safe_ret);
	return primary ? primary : safe_ret;
}

static int radar_speaker_startup(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	int ret;

	ret = mt8163_afe_select_amp(&priv->afe_pdev->dev, false);
	if (ret)
		return ret;
	priv->amp_enable = 0;
	/* Stock Puffin ext_speaker_output uses the board DacMux Off path. */
	ret = mt8163_afe_select_dac(&priv->afe_pdev->dev, false);
	if (ret)
		return ret;
	priv->dac_route = 0;
	return radar_speaker_enable_clocks(priv);
}

static void radar_speaker_shutdown(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	int ret;

	ret = radar_speaker_safe(priv);
	if (ret)
		dev_err(priv->card.dev, "Speaker shutdown cleanup failed: %d\n",
			ret);
}

static int radar_speaker_apply_profile(struct snd_soc_component *component)
{
	size_t i;
	int ret;

	for (i = 0; i < ARRAY_SIZE(radar_puffin_ext_speaker_profile); i++) {
		ret = snd_soc_component_write(
			component, radar_puffin_ext_speaker_profile[i].reg,
			radar_puffin_ext_speaker_profile[i].def);
		if (ret < 0) {
			dev_err(component->dev,
				"Puffin speaker profile write %zu failed: %d\n",
				i, ret);
			return ret;
		}
	}

	/* The working 3.18 sequence applies this after the coefficient table. */
	ret = snd_soc_component_write(component, RADAR_DRC_CTRL1,
				      RADAR_DRC_DISABLED);
	if (ret < 0)
		dev_err(component->dev,
			"Puffin speaker DRC disable failed: %d\n", ret);
	return ret;
}

static int radar_speaker_hw_params(struct snd_pcm_substream *substream,
				   struct snd_pcm_hw_params *params)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	struct snd_soc_dai *codec_dai = asoc_rtd_to_codec(rtd, 0);
	int ret;

	ret = radar_speaker_enable_clocks(priv);
	if (ret)
		return radar_speaker_fail_safe(priv, ret, "hw_params");
	if (params_rate(params) != 48000 || params_channels(params) != 2 ||
	    params_format(params) != SNDRV_PCM_FORMAT_S16_LE) {
		ret = -EINVAL;
		goto fail;
	}

	ret = snd_soc_dai_set_fmt(codec_dai, SND_SOC_DAIFMT_I2S |
				  SND_SOC_DAIFMT_NB_NF |
				  SND_SOC_DAIFMT_CBC_CFC);
	if (ret)
		goto fail;
	ret = snd_soc_dai_set_sysclk(codec_dai, 0, RADAR_MCLK_RATE,
				     SND_SOC_CLOCK_IN);
	if (!ret)
		return 0;
fail:
	return radar_speaker_fail_safe(priv, ret, "hw_params");
}

static int radar_speaker_hw_free(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);

	return radar_speaker_safe(priv);
}

static int radar_speaker_prepare(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	struct snd_soc_component *component =
		asoc_rtd_to_codec(rtd, 0)->component;
	int ret;

	ret = radar_speaker_enable_clocks(priv);
	if (ret)
		return radar_speaker_fail_safe(priv, ret, "prepare");
	/*
	 * Match the 3.18 hw_params ordering exactly: PLL/dividers FIRST,
	 * then PRB/profile, then HEADSTART, then unmute.  The codec's
	 * internal clock tree must be configured before the DAC processing
	 * block activates, otherwise DACFLAG2 stays 0x44 (right DAC only)
	 * instead of 0x88 (both DACs running).
	 *
	 * Step 1: Override the CCF clock solver's PLL/divider values with
	 * the proven 3.18 hardcoded table (P=5,J=48,D=0,NDAC=5,MDAC=3,
	 * BCLK=1).  The 6.1 solver picks P=1,J=10,D=2400 which does not
	 * produce a working DAC datapath.
	 */
	ret = snd_soc_component_write(component, RADAR_PLLPR, RADAR_PLLPR_318);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_PLLJ, RADAR_PLLJ_318);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_PLLD_MSB, 0);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_PLLD_LSB, 0);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_NDAC, RADAR_NDAC_318);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_MDAC, RADAR_MDAC_318);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_DOSR_MSB,
				      RADAR_DOSR_318 >> 8);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_DOSR_LSB,
				      RADAR_DOSR_318 & 0xff);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_BCLKN, RADAR_BCLKN_318);
	if (ret < 0)
		goto fail;
	/*
	 * Step 2: Enable DAC soft-stepping (3.18 DACSETUP=0xd6 vs 6.1
	 * default 0xd4).
	 */
	ret = snd_soc_component_update_bits(component, RADAR_DACSETUP,
					    RADAR_DAC_SOFT_STEP,
					    RADAR_DAC_SOFT_STEP);
	if (ret < 0)
		goto fail;
	/*
	 * Step 3: Select PRB_P2 and load the board's calibrated biquad
	 * coefficients.  The non-zero profile is required: zeroing it
	 * (an earlier experiment) silenced the output entirely.
	 */
	ret = snd_soc_component_write(component, RADAR_DAC_PROCESSING_BLOCK,
				      RADAR_PUFFIN_DAC_PROCESSING_BLOCK);
	if (ret < 0)
		goto fail;
	ret = radar_speaker_apply_profile(component);
	if (ret)
		goto fail;
	ret = radar_apply_dac_route(priv);
	if (ret < 0)
		goto fail;
	/*
	 * Step 4: HP amp soft-route startup delay (0x1d), matching 3.18.
	 */
	ret = snd_soc_component_write(component, RADAR_DAC_HEADSTART,
				      RADAR_HP_AMP_SOFT_STARTUP);
	if (ret < 0)
		goto fail;
	/*
	 * Step 5: Unmute the HP analog output drivers (bit 6).  The codec
	 * resets with HPLGAIN/HPRGAIN=0x40 (muted); 3.18 clears this in
	 * hw_params.
	 */
	ret = snd_soc_component_update_bits(component, RADAR_HPLGAIN,
					    RADAR_HP_DRIVER_MUTE, 0);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_update_bits(component, RADAR_HPRGAIN,
					    RADAR_HP_DRIVER_MUTE, 0);
	if (ret < 0)
		goto fail;
	/*
	 * Step 6: Set DAC digital volume to 0 dB (full volume).  The
	 * "PCM Playback Volume" mixer control maps to -60.5 dB attenuation;
	 * 3.18 leaves DACVOL=0x00 (0 dB, full volume).
	 */
	ret = snd_soc_component_write(component, RADAR_LDACVOL, 0);
	if (ret < 0)
		goto fail;
	ret = snd_soc_component_write(component, RADAR_RDACVOL, 0);
	if (ret < 0)
		goto fail;
	return 0;
fail:
	return radar_speaker_fail_safe(priv, ret, "prepare");
}

static int radar_speaker_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	struct snd_soc_component *component =
		asoc_rtd_to_codec(rtd, 0)->component;
	int ret;

	if (cmd != SNDRV_PCM_TRIGGER_START &&
	    cmd != SNDRV_PCM_TRIGGER_RESUME)
		return 0;

	/*
	 * Re-write the 3.18 PLL/divider values on trigger START.  The 6.1
	 * codec driver's CCF clock framework (aic32x4_setup_clocks in
	 * hw_params, set_bias_level on DAPM power-up) writes its own
	 * PLL/divider values that differ from the proven 3.18 table.
	 * Writing here, after all CCF/DAPM machinery has settled, ensures
	 * our values are the final ones in the hardware.  This is required
	 * to get DACFLAG2=0x88 (both DACs running) instead of 0x44.
	 */
	ret = snd_soc_component_write(component, RADAR_PLLPR, RADAR_PLLPR_318);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_PLLJ, RADAR_PLLJ_318);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_PLLD_MSB, 0);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_PLLD_LSB, 0);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_NDAC, RADAR_NDAC_318);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_MDAC, RADAR_MDAC_318);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_DOSR_MSB,
				      RADAR_DOSR_318 >> 8);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_DOSR_LSB,
				      RADAR_DOSR_318 & 0xff);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_BCLKN, RADAR_BCLKN_318);
	if (ret < 0)
		return ret;

	/* Re-assert DACVOL=0 (full volume) in case mixer controls changed it. */
	ret = snd_soc_component_write(component, RADAR_LDACVOL, 0);
	if (ret < 0)
		return ret;
	ret = snd_soc_component_write(component, RADAR_RDACVOL, 0);
	if (ret < 0)
		return ret;

	return 0;
}

static const struct snd_soc_ops radar_speaker_ops = {
	.startup = radar_speaker_startup,
	.shutdown = radar_speaker_shutdown,
	.hw_params = radar_speaker_hw_params,
	.hw_free = radar_speaker_hw_free,
	.prepare = radar_speaker_prepare,
	.trigger = radar_speaker_trigger,
};

static int radar_i2s1_startup(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	int ret;

	ret = mt8163_afe_select_i2s(&priv->afe_pdev->dev, true);
	if (ret)
		return ret;
	ret = mt8163_afe_select_mclk(&priv->afe_pdev->dev);
	if (ret)
		goto idle_i2s;
	ret = mt8163_mclk_enable(priv->mclk);
	if (ret)
		goto idle_i2s;
	return 0;

idle_i2s:
	mt8163_afe_select_i2s(&priv->afe_pdev->dev, false);
	return ret;
}

static void radar_i2s1_shutdown(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);

	mt8163_afe_select_amp(&priv->afe_pdev->dev, false);
	mt8163_mclk_disable(priv->mclk);
	mt8163_afe_select_i2s(&priv->afe_pdev->dev, false);
}

static const struct snd_soc_ops radar_i2s1_ops = {
	.startup = radar_i2s1_startup,
	.shutdown = radar_i2s1_shutdown,
};

static int radar_capture_hw_params(struct snd_pcm_substream *substream,
				   struct snd_pcm_hw_params *params)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	struct snd_soc_dai *codec_dai = asoc_rtd_to_codec(rtd, 0);
	unsigned int rx_mask;
	int slot_width;
	int ret;

	if (params_rate(params) != 16000 ||
	    params_channels(params) != 9 ||
	    params_format(params) != SNDRV_PCM_FORMAT_S24_3LE)
		return -EINVAL;

	ret = snd_soc_dai_set_pll(codec_dai, RADAR_ADC_PLL_BCLK,
				  RADAR_ADC_PLL_MCLK, RADAR_MCLK_RATE,
				  params_rate(params));
	if (ret)
		return ret;
	ret = snd_soc_dai_set_fmt(codec_dai, SND_SOC_DAIFMT_DSP_B |
				  SND_SOC_DAIFMT_NB_NF |
				  SND_SOC_DAIFMT_CBP_CFP);
	if (ret)
		return ret;

	rx_mask = priv->linein_adc ? 0x40 : 0x7f;
	slot_width = priv->linein_adc ? 0 : snd_pcm_format_width(params_format(params));
	return snd_soc_dai_set_tdm_slot(codec_dai, 0, rx_mask,
					params_channels(params), slot_width);
}

static int radar_capture_startup(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);
	int ret;

	ret = mt8163_afe_select_mclk(&priv->afe_pdev->dev);
	if (ret)
		return ret;
	return mt8163_mclk_enable(priv->mclk);
}

static void radar_capture_shutdown(struct snd_pcm_substream *substream)
{
	struct snd_soc_pcm_runtime *rtd = asoc_substream_to_rtd(substream);
	struct radar_card *priv = snd_soc_card_get_drvdata(rtd->card);

	mt8163_mclk_disable(priv->mclk);
}

static const struct snd_soc_ops radar_capture_ops = {
	.startup = radar_capture_startup,
	.shutdown = radar_capture_shutdown,
	.hw_params = radar_capture_hw_params,
};

static void radar_put_nodes(void *data)
{
	struct radar_card *priv = data;

	if (priv->afe_pdev)
		put_device(&priv->afe_pdev->dev);
	of_node_put(priv->afe_np);
	of_node_put(priv->spi_np);
	of_node_put(priv->dac_np);
	of_node_put(priv->adc_np);
}

static int radar_find_components(struct device *dev, struct radar_card *priv)
{
	int ret;

	priv->afe_np = of_find_compatible_node(NULL, NULL,
					       RADAR_AFE_COMPATIBLE);
	priv->spi_np = of_find_compatible_node(NULL, NULL,
					       RADAR_SPI_COMPATIBLE);
	priv->dac_np = of_find_compatible_node(NULL, NULL,
					       RADAR_DAC_COMPATIBLE);
	priv->adc_np = of_find_compatible_node(NULL, NULL,
					       RADAR_ADC_COMPATIBLE);
	if (!priv->afe_np || !priv->spi_np || !priv->dac_np || !priv->adc_np) {
		ret = -EPROBE_DEFER;
		goto put_nodes;
	}

	priv->afe_pdev = of_find_device_by_node(priv->afe_np);
	if (!priv->afe_pdev) {
		ret = -EPROBE_DEFER;
		goto put_nodes;
	}
	return devm_add_action_or_reset(dev, radar_put_nodes, priv);

put_nodes:
	radar_put_nodes(priv);
	return ret;
}

static void radar_setup_link(struct radar_card *priv, int index,
			     const char *name, const char *stream, int pcm_id,
			     struct device_node *cpu_np, const char *cpu_dai,
			     struct device_node *codec_np, const char *codec_dai)
{
	struct snd_soc_dai_link *link = &priv->links[index];

	priv->cpus[index].of_node = cpu_np;
	priv->cpus[index].dai_name = cpu_dai;
	priv->platforms[index].of_node = cpu_np;
	priv->codecs[index].of_node = codec_np;
	priv->codecs[index].dai_name = codec_dai;
	link->name = name;
	link->stream_name = stream;
	link->id = pcm_id;
	link->cpus = &priv->cpus[index];
	link->num_cpus = 1;
	link->platforms = &priv->platforms[index];
	link->num_platforms = 1;
	link->codecs = &priv->codecs[index];
	link->num_codecs = 1;
}

static int radar_card_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct radar_card *priv;
	int ret;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;
	ret = radar_find_components(dev, priv);
	if (ret)
		return ret;
	priv->mclk = mt8163_mclk_get(dev);
	if (IS_ERR(priv->mclk))
		return dev_err_probe(dev, PTR_ERR(priv->mclk),
				     "cannot acquire codec MCLK\n");

	radar_setup_link(priv, RADAR_LINK_DAC, "TI_DAC_Playback",
			 "TLV320AIC3204 Playback", 23, priv->afe_np,
			 RADAR_DL1_DAI, priv->dac_np, RADAR_DAC_DAI);
	priv->links[RADAR_LINK_DAC].playback_only = 1;
	priv->links[RADAR_LINK_DAC].nonatomic = 1;
	priv->links[RADAR_LINK_DAC].ignore_pmdown_time = 1;
	priv->links[RADAR_LINK_DAC].init = radar_speaker_init;
	priv->links[RADAR_LINK_DAC].ops = &radar_speaker_ops;

	radar_setup_link(priv, RADAR_LINK_CAPTURE, "AMZN_SPI_Capture",
			 "TLV320AIC3101 Capture", 24, priv->spi_np,
			 RADAR_SPI_DAI, priv->adc_np, RADAR_ADC_DAI);
	priv->links[RADAR_LINK_CAPTURE].capture_only = 1;
	priv->links[RADAR_LINK_CAPTURE].ignore_pmdown_time = 1;
	priv->links[RADAR_LINK_CAPTURE].ops = &radar_capture_ops;

	radar_setup_link(priv, RADAR_LINK_I2S1, "I2S1_Playback",
			 "I2S1_Playback", 25, priv->afe_np, RADAR_DL1_DAI,
			 NULL, "snd-soc-dummy-dai");
	priv->codecs[RADAR_LINK_I2S1].name = "snd-soc-dummy";
	priv->links[RADAR_LINK_I2S1].playback_only = 1;
	priv->links[RADAR_LINK_I2S1].nonatomic = 1;
	priv->links[RADAR_LINK_I2S1].ignore_pmdown_time = 1;
	priv->links[RADAR_LINK_I2S1].ops = &radar_i2s1_ops;

	priv->card.name = RADAR_CARD_NAME;
	priv->card.owner = THIS_MODULE;
	priv->card.dev = dev;
	priv->card.dai_link = priv->links;
	priv->card.num_links = ARRAY_SIZE(priv->links);
	priv->card.controls = radar_controls;
	priv->card.num_controls = ARRAY_SIZE(radar_controls);
	snd_soc_card_set_drvdata(&priv->card, priv);
	platform_set_drvdata(pdev, priv);

	ret = devm_snd_soc_register_card(dev, &priv->card);
	if (ret)
		return dev_err_probe(dev, ret,
				     "cannot register Radar-Puffin card\n");
	return 0;
}

static const struct of_device_id radar_card_of_match[] = {
	{ .compatible = "mediatek,mt8163-soc-codec-63xx" },
	{ }
};
MODULE_DEVICE_TABLE(of, radar_card_of_match);

static struct platform_driver radar_card_driver = {
	.probe = radar_card_probe,
	.driver = {
		.name = "mt8163-radar-puffin-audio",
		.of_match_table = radar_card_of_match,
	},
};
module_platform_driver(radar_card_driver);

MODULE_DESCRIPTION("Amazon Radar-Puffin MT8163 ASoC machine driver");
MODULE_LICENSE("GPL");
