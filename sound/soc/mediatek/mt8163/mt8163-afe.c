// SPDX-License-Identifier: GPL-2.0-only
/*
 * Narrow MT8163 AFE support for the Radar-Puffin DL1 playback path.
 *
 * The register programming follows the MT8163 vendor DL1/I2S1 path.  This
 * deliberately does not register the unrelated modem, Bluetooth, FM or HDMI
 * front ends present in the vendor tree.
 */

#include <linux/clk.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/mfd/syscon.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/pinctrl/consumer.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/regmap.h>
#include <linux/uaccess.h>

#include <sound/pcm.h>
#include <sound/pcm_params.h>
#include <sound/soc.h>

#include "mt8163-afe.h"

#define MT8163_DL1_DAI_NAME		"mt-soc-dl1dai-driver"
#define MT8163_AFE_COMPONENT_NAME	"mt-soc-i2s0dl1-pcm"

#define AUDIO_TOP_CON0			0x0000
#define AFE_DAC_CON0			0x0010
#define AFE_DAC_CON1			0x0014
#define AFE_I2S_CON			0x0018
#define AFE_CONN0			0x0020
#define AFE_CONN1			0x0024
#define AFE_CONN2			0x0028
#define AFE_I2S_CON1			0x0034
#define AFE_DL1_BASE			0x0040
#define AFE_DL1_CUR			0x0044
#define AFE_DL1_END			0x0048
#define AFE_I2S_CON3			0x004c
#define AFE_CONN_24BIT			0x006c
#define AFE_ADDA_DL_SRC2_CON0		0x0108
#define AFE_ADDA_DL_SRC2_CON1		0x010c
#define AFE_ADDA_UL_DL_CON0		0x0124
#define AFE_ADDA_PREDIS_CON0		0x0260
#define AFE_ADDA_PREDIS_CON1		0x0264
#define AFE_IRQ_MCU_CON			0x03a0
#define AFE_IRQ_STATUS			0x03a4
#define AFE_IRQ_CLR			0x03a8
#define AFE_IRQ_CNT1			0x03ac
#define AFE_MEMIF_PBUF_SIZE		0x03d8
#define FPGA_CFG1			0x04c0

#define MT8163_POWER_TOP_OFFSET		0x029c
#define MT8163_POWER_TOP_AUDIO		0x0000000d

/* Topckgen audio clock registers (MT8163 vendor AUDIO_CLK_* values). */
#define MT8163_TOPCKGEN_AUDIO_CFG_6	0x00a0
#define MT8163_TOPCKGEN_AUDDIV_0		0x05a0
#define MT8163_TOPCKGEN_AUDDIV_1		0x05a4
#define MT8163_TOPCKGEN_AUDDIV0_PDN_MASK	(BIT(1) | BIT(3) | BIT(5))
#define MT8163_TOPCKGEN_AUDDIV0_SOURCE_MASK	(BIT(9) | BIT(11))
#define MT8163_TOPCKGEN_AUDDIV0_DIV_MASK	GENMASK(30, 28)
#define MT8163_TOPCKGEN_AUDDIV1_I2S1_MASK	GENMASK(14, 8)
#define MT8163_TOPCKGEN_AUDDIV1_I2S3_MASK	GENMASK(30, 24)
#define MT8163_TOPCKGEN_AUDDIV0_DIV4		(3U << 28)
#define MT8163_TOPCKGEN_I2S_MCLK_DIV		15U

/* AP_PLL_CON5 is in the MT8163 APMIXED block, not the AFE regmap. */
#define MT8163_APMIXED_AP_PLL_CON5	0x0014
#define MT8163_APMIXED_APLL2_TUNER	BIT(1)
#define AFE_APLL2_TUNER_CFG		0x03f4
#define AFE_APLL2_TUNER_CFG_VALUE	0x00000035
#define AFE_APLL2_TUNER_CFG_MASK	0x0000fff7

#define AFE_RATE_48K			10
#define AFE_BUS_INIT			BIT(14)
#define AFE_CLOCK_GATE_MASK		(BIT(2) | BIT(9) | BIT(18) | BIT(25) | BIT(26))
#define AFE_GLOBAL_ENABLE		BIT(0)
#define AFE_DL1_ENABLE			BIT(1)
#define AFE_OTHER_MEMIF_ENABLE_MASK	GENMASK(10, 2)
#define AFE_DL1_MONO			BIT(21)
#define AFE_DL1_FORMAT_MASK		GENMASK(17, 16)
#define AFE_IRQ1_ENABLE			BIT(0)
#define AFE_I2S_ENABLE			BIT(0)
#define AFE_I2S_FORMAT_I2S		BIT(3)
#define AFE_I2S_WORD_32			BIT(1)
#define AFE_ADDA_DL_ENABLE		BIT(0)
#define AFE_ADDA_ENABLE			BIT(0)
#define AFE_ADDA_DL_SRC2_48K		0x83001802
#define AFE_ADDA_DL_SRC2_GAIN		0xf74f0000
#define FPGA_CFG1_DAC_PIN_SELECT		BIT(4)
#define AFE_I05_TO_O00			BIT(5)
#define AFE_I06_TO_O01			BIT(22)
#define AFE_I05_TO_O03			BIT(21)
#define AFE_I06_TO_O04			BIT(6)

#define MT8163_PCM_RATE			48000
#define MT8163_PCM_BUFFER_MAX		0x6000
#define MT8163_PCM_ALIGN			64

enum mt8163_pin {
	MT8163_PIN_I2S_IDLE,
	MT8163_PIN_I2S_ACTIVE,
	MT8163_PIN_AMP_ON,
	MT8163_PIN_AMP_OFF,
	MT8163_PIN_MCLK,
	MT8163_PIN_DAC_ON,
	MT8163_PIN_DAC_OFF,
	MT8163_PIN_NUM,
};

static const char * const mt8163_pin_names[MT8163_PIN_NUM] = {
	[MT8163_PIN_I2S_IDLE] = "audi2s1-mode0",
	[MT8163_PIN_I2S_ACTIVE] = "audi2s1-mode1",
	[MT8163_PIN_AMP_ON] = "extamp-pullhigh",
	[MT8163_PIN_AMP_OFF] = "extamp-pulllow",
	[MT8163_PIN_MCLK] = "cmmclk-mclk",
	[MT8163_PIN_DAC_ON] = "extamp-dacmux-pullhigh",
	[MT8163_PIN_DAC_OFF] = "extamp-dacmux-pulllow",
};

struct mt8163_afe {
	struct device *dev;
	struct regmap *regmap;
	void __iomem *sram;
	dma_addr_t sram_phys;
	size_t sram_size;
	struct clk *infra;
	struct clk *audio_mux;
	struct clk *audio_intbus_mux;
	struct clk *aud_mux2;
	struct clk *apll2;
	void __iomem *topckgen;
	void __iomem *apmixedsys;
	struct pinctrl *pinctrl;
	struct pinctrl_state *pins[MT8163_PIN_NUM];
	struct snd_pcm_substream *substream;
	struct mutex lock;
	spinlock_t irq_lock;
	int irq;
	bool clocks_enabled;
	bool running;
};

static const struct snd_pcm_hardware mt8163_pcm_hardware = {
	.info = SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER,
	.formats = SNDRV_PCM_FMTBIT_S16_LE,
	.rates = SNDRV_PCM_RATE_48000,
	.rate_min = MT8163_PCM_RATE,
	.rate_max = MT8163_PCM_RATE,
	.channels_min = 1,
	.channels_max = 2,
	.buffer_bytes_max = MT8163_PCM_BUFFER_MAX,
	.period_bytes_min = MT8163_PCM_ALIGN,
	.period_bytes_max = MT8163_PCM_BUFFER_MAX / 2,
	.periods_min = 2,
	.periods_max = MT8163_PCM_BUFFER_MAX / MT8163_PCM_ALIGN,
};

static int mt8163_afe_pin(struct mt8163_afe *afe, enum mt8163_pin pin)
{
	if (!afe || !afe->pinctrl || !afe->pins[pin])
		return -ENODEV;

	return pinctrl_select_state(afe->pinctrl, afe->pins[pin]);
}

static struct mt8163_afe *mt8163_afe_from_dev(struct device *dev)
{
	if (!dev)
		return NULL;
	return dev_get_drvdata(dev);
}

int mt8163_afe_select_i2s(struct device *dev, bool enable)
{
	struct mt8163_afe *afe = mt8163_afe_from_dev(dev);

	return mt8163_afe_pin(afe, enable ? MT8163_PIN_I2S_ACTIVE :
			     MT8163_PIN_I2S_IDLE);
}
EXPORT_SYMBOL_GPL(mt8163_afe_select_i2s);

int mt8163_afe_select_amp(struct device *dev, bool enable)
{
	struct mt8163_afe *afe = mt8163_afe_from_dev(dev);

	return mt8163_afe_pin(afe, enable ? MT8163_PIN_AMP_ON :
			     MT8163_PIN_AMP_OFF);
}
EXPORT_SYMBOL_GPL(mt8163_afe_select_amp);

int mt8163_afe_select_dac(struct device *dev, bool enable)
{
	struct mt8163_afe *afe = mt8163_afe_from_dev(dev);

	return mt8163_afe_pin(afe, enable ? MT8163_PIN_DAC_ON :
			     MT8163_PIN_DAC_OFF);
}
EXPORT_SYMBOL_GPL(mt8163_afe_select_dac);

int mt8163_afe_select_mclk(struct device *dev)
{
	return mt8163_afe_pin(mt8163_afe_from_dev(dev), MT8163_PIN_MCLK);
}
EXPORT_SYMBOL_GPL(mt8163_afe_select_mclk);

static int mt8163_afe_safe(struct mt8163_afe *afe)
{
	int ret, first = 0;

	ret = mt8163_afe_select_amp(afe->dev, false);
	if (ret)
		first = ret;
	ret = mt8163_afe_select_dac(afe->dev, false);
	if (ret && !first)
		first = ret;
	ret = mt8163_afe_select_i2s(afe->dev, false);
	if (ret && !first)
		first = ret;
	return first;
}

static void mt8163_afe_mmio_update_bits(void __iomem *base, u32 reg,
					 u32 mask, u32 val)
{
	u32 old, new;

	old = readl(base + reg);
	new = (old & ~mask) | (val & mask);
	if (new != old)
		writel(new, base + reg);
	/* Ensure clock-control writes have reached the peripheral before use. */
	readl(base + reg);
}

static int mt8163_afe_audio_clocks_setup(struct mt8163_afe *afe)
{
	int ret;

	/* Match EnableApll2() from the vendor 3.18 driver. */
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDIO_CFG_6,
				    BIT(24), BIT(24));
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_0,
				    BIT(1), BIT(1));
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_0,
				    MT8163_TOPCKGEN_AUDDIV0_DIV_MASK,
				    MT8163_TOPCKGEN_AUDDIV0_DIV4);

	/* The DL1 path uses both I2S1 and I2S3 divider outputs. */
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_0,
				    MT8163_TOPCKGEN_AUDDIV0_SOURCE_MASK, 0);
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_1,
				    MT8163_TOPCKGEN_AUDDIV1_I2S1_MASK,
				    MT8163_TOPCKGEN_I2S_MCLK_DIV << 8);
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_1,
				    MT8163_TOPCKGEN_AUDDIV1_I2S3_MASK,
				    MT8163_TOPCKGEN_I2S_MCLK_DIV << 24);

	ret = regmap_update_bits(afe->regmap, AFE_APLL2_TUNER_CFG,
				 AFE_APLL2_TUNER_CFG_MASK,
				 AFE_APLL2_TUNER_CFG_VALUE);
	if (ret)
		return ret;

	/* EnableApll2Tuner() also sets AP_PLL_CON5 bit 1. */
	mt8163_afe_mmio_update_bits(afe->apmixedsys,
				    MT8163_APMIXED_AP_PLL_CON5,
				    MT8163_APMIXED_APLL2_TUNER,
				    MT8163_APMIXED_APLL2_TUNER);

	/* Enable the APLL2-derived 24 MHz clock and divider outputs. */
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_0,
				    MT8163_TOPCKGEN_AUDDIV0_PDN_MASK, 0);
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDIO_CFG_6,
				    BIT(31), 0);

	return 0;
}

static void mt8163_afe_audio_clocks_teardown(struct mt8163_afe *afe)
{
	/* Stop the generated clocks before dropping their AFE gate. */
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDIO_CFG_6,
				    BIT(31), BIT(31));
	mt8163_afe_mmio_update_bits(afe->topckgen,
				    MT8163_TOPCKGEN_AUDDIV_0,
				    MT8163_TOPCKGEN_AUDDIV0_PDN_MASK,
				    MT8163_TOPCKGEN_AUDDIV0_PDN_MASK);
	mt8163_afe_mmio_update_bits(afe->apmixedsys,
				    MT8163_APMIXED_AP_PLL_CON5,
				    MT8163_APMIXED_APLL2_TUNER, 0);
}

static int mt8163_afe_clocks_enable(struct mt8163_afe *afe)
{
	int ret;

	if (afe->clocks_enabled)
		return 0;

	ret = pm_runtime_resume_and_get(afe->dev);
	if (ret < 0)
		return ret;
	ret = clk_prepare_enable(afe->infra);
	if (ret)
		goto put_pm;
	ret = clk_prepare_enable(afe->audio_intbus_mux);
	if (ret)
		goto disable_infra;
	ret = clk_prepare_enable(afe->audio_mux);
	if (ret)
		goto disable_intbus;

	/*
	 * Route aud_2_sel to APLL2 through CCF.  The raw divider and tuner
	 * programming below is also required: the 6.1 clock driver exposes
	 * the mux and gate clocks, but not the MT8163 AUDDIV registers.
	 */
	if (afe->aud_mux2 && afe->apll2) {
		ret = clk_set_parent(afe->aud_mux2, afe->apll2);
		if (ret)
			goto disable_audio_mux;
		/*
		 * apll2_ck is a fixed-factor (1:1) of aud2pll with no
		 * CLK_SET_RATE_PARENT, so set_rate can legitimately fail;
		 * the PLL default is already 98.304 MHz (verified live).
		 * Best-effort only: a failure here must not kill the path.
		 */
		ret = clk_set_rate(afe->apll2, 98304000);
		if (ret)
			dev_warn(afe->dev,
				 "aud2pll set_rate 98304000 failed (%d), using default\n",
				 ret);
		ret = clk_prepare_enable(afe->aud_mux2);
		if (ret)
			goto disable_audio_mux;
		ret = mt8163_afe_audio_clocks_setup(afe);
		if (ret)
			goto teardown_aud_clocks;
	}

	regmap_update_bits(afe->regmap, AUDIO_TOP_CON0,
			   AFE_BUS_INIT, AFE_BUS_INIT);
	regmap_update_bits(afe->regmap, AUDIO_TOP_CON0,
			   AFE_CLOCK_GATE_MASK, 0);
	afe->clocks_enabled = true;
	return 0;

teardown_aud_clocks:
	mt8163_afe_audio_clocks_teardown(afe);
	clk_disable_unprepare(afe->aud_mux2);
disable_audio_mux:
	clk_disable_unprepare(afe->audio_mux);
disable_intbus:
	clk_disable_unprepare(afe->audio_intbus_mux);
disable_infra:
	clk_disable_unprepare(afe->infra);
put_pm:
	pm_runtime_put_sync(afe->dev);
	return ret;
}

static void mt8163_afe_clocks_disable(struct mt8163_afe *afe)
{
	if (!afe->clocks_enabled)
		return;

	regmap_update_bits(afe->regmap, AUDIO_TOP_CON0,
			   AFE_CLOCK_GATE_MASK, AFE_CLOCK_GATE_MASK);
	mt8163_afe_audio_clocks_teardown(afe);
	if (afe->aud_mux2)
		clk_disable_unprepare(afe->aud_mux2);
	clk_disable_unprepare(afe->audio_mux);
	clk_disable_unprepare(afe->audio_intbus_mux);
	clk_disable_unprepare(afe->infra);
	pm_runtime_put_sync(afe->dev);
	afe->clocks_enabled = false;
}

static void mt8163_afe_record_error(int *first, int ret)
{
	if (ret && !*first)
		*first = ret;
}

static int mt8163_afe_preserve_error(struct mt8163_afe *afe, int primary,
				     int cleanup, const char *operation)
{
	if (cleanup && primary)
		dev_err(afe->dev,
			"%s cleanup failed: primary=%d cleanup=%d\n",
			operation, primary, cleanup);
	return primary ? primary : cleanup;
}

static int mt8163_afe_quiesce(struct mt8163_afe *afe)
{
	unsigned long flags;
	unsigned int val;
	int first = 0;
	int ret;

	spin_lock_irqsave(&afe->irq_lock, flags);
	afe->running = false;
	spin_unlock_irqrestore(&afe->irq_lock, flags);

	ret = regmap_update_bits(afe->regmap, AFE_IRQ_MCU_CON,
				 AFE_IRQ1_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_write(afe->regmap, AFE_IRQ_CLR, AFE_IRQ1_ENABLE);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_DAC_CON0,
				 AFE_DL1_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_CONN0,
				 AFE_I05_TO_O00 | AFE_I06_TO_O01, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_CONN1,
				 AFE_I05_TO_O03, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_CONN2,
				 AFE_I06_TO_O04, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_I2S_CON,
				 AFE_I2S_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_I2S_CON1,
				 AFE_I2S_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_I2S_CON3,
				 AFE_I2S_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_ADDA_DL_SRC2_CON0,
				 AFE_ADDA_DL_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, AFE_ADDA_UL_DL_CON0,
				 AFE_ADDA_ENABLE, 0);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_update_bits(afe->regmap, FPGA_CFG1,
				 FPGA_CFG1_DAC_PIN_SELECT,
				 FPGA_CFG1_DAC_PIN_SELECT);
	mt8163_afe_record_error(&first, ret);
	ret = regmap_read(afe->regmap, AFE_DAC_CON0, &val);
	mt8163_afe_record_error(&first, ret);
	if (!ret && !(val & AFE_OTHER_MEMIF_ENABLE_MASK)) {
		ret = regmap_update_bits(afe->regmap, AFE_DAC_CON0,
					 AFE_GLOBAL_ENABLE, 0);
		mt8163_afe_record_error(&first, ret);
	}
	return first;
}

static int mt8163_afe_stop(struct mt8163_afe *afe)
{
	int ret;
	int safe_ret;

	ret = mt8163_afe_quiesce(afe);
	safe_ret = mt8163_afe_safe(afe);
	return mt8163_afe_preserve_error(afe, ret, safe_ret, "stop");
}

static irqreturn_t mt8163_afe_irq(int irq, void *data)
{
	struct mt8163_afe *afe = data;
	struct snd_pcm_substream *substream = NULL;
	unsigned long flags;
	unsigned int status;

	if (regmap_read(afe->regmap, AFE_IRQ_STATUS, &status))
		return IRQ_NONE;
	if (!(status & AFE_IRQ1_ENABLE))
		return IRQ_NONE;

	regmap_write(afe->regmap, AFE_IRQ_CLR, AFE_IRQ1_ENABLE);
	spin_lock_irqsave(&afe->irq_lock, flags);
	if (afe->running)
		substream = afe->substream;
	spin_unlock_irqrestore(&afe->irq_lock, flags);
	if (substream)
		snd_pcm_period_elapsed(substream);
	return IRQ_HANDLED;
}

static int mt8163_pcm_open(struct snd_soc_component *component,
			   struct snd_pcm_substream *substream)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	struct snd_pcm_runtime *runtime = substream->runtime;
	int ret;

	if (substream->stream != SNDRV_PCM_STREAM_PLAYBACK)
		return -EINVAL;

	mutex_lock(&afe->lock);
	if (afe->substream) {
		ret = -EBUSY;
		goto unlock;
	}
	ret = mt8163_afe_clocks_enable(afe);
	if (ret)
		goto unlock;

	runtime->hw = mt8163_pcm_hardware;
	runtime->dma_area = (void *)afe->sram;
	runtime->dma_addr = afe->sram_phys;
	runtime->dma_bytes = afe->sram_size;
	ret = snd_pcm_hw_constraint_step(runtime, 0,
					 SNDRV_PCM_HW_PARAM_BUFFER_BYTES,
					 MT8163_PCM_ALIGN);
	if (!ret)
		ret = snd_pcm_hw_constraint_step(runtime, 0,
						 SNDRV_PCM_HW_PARAM_PERIOD_BYTES,
						 MT8163_PCM_ALIGN);
	if (ret) {
		mt8163_afe_clocks_disable(afe);
		goto unlock;
	}
	afe->substream = substream;
unlock:
	mutex_unlock(&afe->lock);
	return ret;
}

static int mt8163_pcm_close(struct snd_soc_component *component,
			    struct snd_pcm_substream *substream)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	unsigned long flags;

	mutex_lock(&afe->lock);
	mt8163_afe_stop(afe);
	synchronize_irq(afe->irq);
	spin_lock_irqsave(&afe->irq_lock, flags);
	afe->substream = NULL;
	spin_unlock_irqrestore(&afe->irq_lock, flags);
	mt8163_afe_safe(afe);
	mt8163_afe_clocks_disable(afe);
	mutex_unlock(&afe->lock);
	return 0;
}

static int mt8163_pcm_hw_params(struct snd_soc_component *component,
				struct snd_pcm_substream *substream,
				struct snd_pcm_hw_params *params)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	size_t bytes = params_buffer_bytes(params);

	if (params_rate(params) != MT8163_PCM_RATE ||
	    (params_channels(params) != 1 && params_channels(params) != 2) ||
	    params_format(params) != SNDRV_PCM_FORMAT_S16_LE ||
	    !bytes || bytes > afe->sram_size ||
	    bytes > MT8163_PCM_BUFFER_MAX ||
	    !IS_ALIGNED(bytes, MT8163_PCM_ALIGN))
		return -EINVAL;

	substream->runtime->dma_area = (void *)afe->sram;
	substream->runtime->dma_addr = afe->sram_phys;
	substream->runtime->dma_bytes = bytes;
	regmap_write(afe->regmap, AFE_DL1_BASE, (u32)afe->sram_phys);
	return regmap_write(afe->regmap, AFE_DL1_END,
			    (u32)(afe->sram_phys + bytes - 1));
}

static int mt8163_pcm_prepare(struct snd_soc_component *component,
			      struct snd_pcm_substream *substream)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	unsigned int i2s_dac = (AFE_RATE_48K << 8) |
			       AFE_I2S_FORMAT_I2S;
	unsigned int i2s_ext = i2s_dac | AFE_I2S_WORD_32;
	int safe_ret;
	int ret;

	memset_io(afe->sram, 0, substream->runtime->dma_bytes);
	ret = mt8163_afe_select_i2s(afe->dev, true);
	if (ret)
		goto safe;
	ret = mt8163_afe_select_mclk(afe->dev);
	if (ret)
		goto safe;
	ret = mt8163_afe_select_dac(afe->dev, false);
	if (ret)
		goto safe;

	ret = regmap_update_bits(afe->regmap, AFE_MEMIF_PBUF_SIZE,
				 AFE_DL1_FORMAT_MASK, 0);
	if (ret)
		goto safe;
	ret = regmap_update_bits(afe->regmap, AFE_DAC_CON1,
				 GENMASK(3, 0), AFE_RATE_48K);
	if (ret)
		goto safe;
	/* Mono playback duplicates the sample to both I2S channels; stereo
	 * streams the two channels directly. */
	ret = regmap_update_bits(afe->regmap, AFE_DAC_CON1, AFE_DL1_MONO,
				 substream->runtime->channels == 1 ?
				 AFE_DL1_MONO : 0);
	if (ret)
		goto safe;
	ret = regmap_update_bits(afe->regmap, AFE_CONN_24BIT,
				 BIT(0) | BIT(1) | BIT(3) | BIT(4), 0);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_ADDA_PREDIS_CON0, 0);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_ADDA_PREDIS_CON1, 0);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_ADDA_DL_SRC2_CON0,
			   AFE_ADDA_DL_SRC2_48K);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_ADDA_DL_SRC2_CON1,
			   AFE_ADDA_DL_SRC2_GAIN);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_I2S_CON, i2s_ext);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_I2S_CON1, i2s_dac);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_I2S_CON3, i2s_ext);
	if (ret)
		goto safe;
	ret = regmap_write(afe->regmap, AFE_IRQ_CNT1,
			   (u32)substream->runtime->period_size);
	if (ret)
		goto safe;
	ret = regmap_update_bits(afe->regmap, AFE_IRQ_MCU_CON,
				 GENMASK(7, 4), AFE_RATE_48K << 4);
	if (ret)
		goto safe;
	ret = mt8163_afe_quiesce(afe);
	if (!ret)
		return 0;
safe:
	safe_ret = mt8163_afe_safe(afe);
	return mt8163_afe_preserve_error(afe, ret, safe_ret, "prepare");
}

static int mt8163_pcm_trigger(struct snd_soc_component *component,
			      struct snd_pcm_substream *substream, int cmd)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	unsigned long flags;
	int safe_ret;
	int ret;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
		ret = regmap_update_bits(afe->regmap, AFE_CONN0,
					 AFE_I05_TO_O00 | AFE_I06_TO_O01,
					 AFE_I05_TO_O00 | AFE_I06_TO_O01);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_CONN1,
					 AFE_I05_TO_O03, AFE_I05_TO_O03);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_CONN2,
					 AFE_I06_TO_O04, AFE_I06_TO_O04);
		if (ret)
			goto fail;
		ret = regmap_write(afe->regmap, AFE_IRQ_CLR,
				   AFE_IRQ1_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_I2S_CON,
					 AFE_I2S_ENABLE, AFE_I2S_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_ADDA_DL_SRC2_CON0,
					 AFE_ADDA_DL_ENABLE,
					 AFE_ADDA_DL_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_I2S_CON1,
					 AFE_I2S_ENABLE, AFE_I2S_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_ADDA_UL_DL_CON0,
					 AFE_ADDA_ENABLE, AFE_ADDA_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, FPGA_CFG1,
					 FPGA_CFG1_DAC_PIN_SELECT, 0);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_I2S_CON3,
					 AFE_I2S_ENABLE, AFE_I2S_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_DAC_CON0,
					 AFE_DL1_ENABLE, AFE_DL1_ENABLE);
		if (ret)
			goto fail;
		ret = regmap_update_bits(afe->regmap, AFE_IRQ_MCU_CON,
					 AFE_IRQ1_ENABLE, AFE_IRQ1_ENABLE);
		if (!ret)
			ret = regmap_update_bits(afe->regmap, AFE_DAC_CON0,
						 AFE_GLOBAL_ENABLE,
						 AFE_GLOBAL_ENABLE);
		if (ret)
			goto fail;
		spin_lock_irqsave(&afe->irq_lock, flags);
		afe->running = true;
		spin_unlock_irqrestore(&afe->irq_lock, flags);
		return 0;
fail:
		safe_ret = mt8163_afe_stop(afe);
		return mt8163_afe_preserve_error(afe, ret, safe_ret,
						 "start");
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
		return mt8163_afe_stop(afe);
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t
mt8163_pcm_pointer(struct snd_soc_component *component,
		   struct snd_pcm_substream *substream)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	unsigned int ptr;
	u32 base = (u32)afe->sram_phys;
	u32 bytes = (u32)substream->runtime->dma_bytes;

	if (regmap_read(afe->regmap, AFE_DL1_CUR, &ptr) ||
	    ptr < base || ptr >= base + bytes)
		return 0;
	return bytes_to_frames(substream->runtime, ptr - base);
}

static int mt8163_pcm_copy_user(struct snd_soc_component *component,
				struct snd_pcm_substream *substream,
				int channel, unsigned long pos,
				void __user *buf, unsigned long bytes)
{
	struct mt8163_afe *afe = snd_soc_component_get_drvdata(component);
	size_t first;
	void *bounce;

	if (!bytes || pos >= substream->runtime->dma_bytes ||
	    bytes > substream->runtime->dma_bytes)
		return -EINVAL;
	bounce = memdup_user(buf, bytes);
	if (IS_ERR(bounce))
		return PTR_ERR(bounce);
	first = min_t(size_t, bytes, substream->runtime->dma_bytes - pos);
	memcpy_toio(afe->sram + pos, bounce, first);
	if (first < bytes)
		memcpy_toio(afe->sram, bounce + first, bytes - first);
	kfree(bounce);
	return 0;
}

static const struct snd_soc_component_driver mt8163_afe_component = {
	.name = MT8163_AFE_COMPONENT_NAME,
	.open = mt8163_pcm_open,
	.close = mt8163_pcm_close,
	.hw_params = mt8163_pcm_hw_params,
	.prepare = mt8163_pcm_prepare,
	.trigger = mt8163_pcm_trigger,
	.pointer = mt8163_pcm_pointer,
	.copy_user = mt8163_pcm_copy_user,
	.use_dai_pcm_id = true,
	.legacy_dai_naming = true,
};

static struct snd_soc_dai_driver mt8163_afe_dai = {
	.name = MT8163_DL1_DAI_NAME,
	.playback = {
		.stream_name = "DL1 Playback",
		.channels_min = 1,
		.channels_max = 2,
		.rates = SNDRV_PCM_RATE_48000,
		.formats = SNDRV_PCM_FMTBIT_S16_LE,
	},
};

static const struct regmap_config mt8163_afe_regmap_config = {
	.reg_bits = 32,
	.reg_stride = 4,
	.val_bits = 32,
	.max_register = 0x9000 - 4,
	.cache_type = REGCACHE_NONE,
};

static int mt8163_afe_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct resource *sram_res;
	struct regmap *scpsys;
	struct device_node *np;
	struct mt8163_afe *afe;
	void __iomem *base;
	int i, ret;

	afe = devm_kzalloc(dev, sizeof(*afe), GFP_KERNEL);
	if (!afe)
		return -ENOMEM;
	afe->dev = dev;
	mutex_init(&afe->lock);
	spin_lock_init(&afe->irq_lock);

	base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(base))
		return PTR_ERR(base);
	afe->regmap = devm_regmap_init_mmio(dev, base,
					    &mt8163_afe_regmap_config);
	if (IS_ERR(afe->regmap))
		return PTR_ERR(afe->regmap);

	np = of_find_compatible_node(NULL, NULL, "mediatek,mt8163-topckgen");
	if (!np)
		return dev_err_probe(dev, -ENODEV, "missing MT8163 topckgen\n");
	afe->topckgen = devm_of_iomap(dev, np, 0, NULL);
	of_node_put(np);
	if (IS_ERR(afe->topckgen))
		return dev_err_probe(dev, PTR_ERR(afe->topckgen),
				     "cannot map MT8163 topckgen\n");

	np = of_find_compatible_node(NULL, NULL, "mediatek,mt8163-apmixedsys");
	if (!np)
		return dev_err_probe(dev, -ENODEV, "missing MT8163 apmixedsys\n");
	afe->apmixedsys = devm_of_iomap(dev, np, 0, NULL);
	of_node_put(np);
	if (IS_ERR(afe->apmixedsys))
		return dev_err_probe(dev, PTR_ERR(afe->apmixedsys),
				     "cannot map MT8163 apmixedsys\n");

	scpsys = syscon_regmap_lookup_by_compatible("mediatek,mt8163-scpsys");
	if (IS_ERR(scpsys))
		return dev_err_probe(dev, PTR_ERR(scpsys),
				     "cannot find SCPSYS regmap\n");
	ret = regmap_write(scpsys, MT8163_POWER_TOP_OFFSET,
			   MT8163_POWER_TOP_AUDIO);
	if (ret)
		return ret;

	sram_res = platform_get_resource(pdev, IORESOURCE_MEM, 1);
	if (!sram_res)
		return -ENODEV;
	afe->sram_size = min_t(size_t, resource_size(sram_res),
			       MT8163_PCM_BUFFER_MAX);
	afe->sram = devm_ioremap_resource(dev, sram_res);
	if (IS_ERR(afe->sram))
		return PTR_ERR(afe->sram);
	afe->sram_phys = sram_res->start;

	afe->infra = devm_clk_get(dev, "aud_infra_clk");
	if (IS_ERR(afe->infra))
		return dev_err_probe(dev, PTR_ERR(afe->infra),
				     "missing aud_infra_clk\n");
	afe->audio_mux = devm_clk_get(dev, "top_mux_audio");
	if (IS_ERR(afe->audio_mux))
		return dev_err_probe(dev, PTR_ERR(afe->audio_mux),
				     "missing top_mux_audio\n");
	afe->audio_intbus_mux = devm_clk_get(dev, "top_mux_audio_intbus");
	if (IS_ERR(afe->audio_intbus_mux))
		return dev_err_probe(dev, PTR_ERR(afe->audio_intbus_mux),
				     "missing top_mux_audio_intbus\n");
	afe->aud_mux2 = devm_clk_get(dev, "aud_mux2_clk");
	if (IS_ERR(afe->aud_mux2))
		return dev_err_probe(dev, PTR_ERR(afe->aud_mux2),
				     "missing aud_mux2_clk\n");
	afe->apll2 = devm_clk_get(dev, "apmixed_apll2_clk");
	if (IS_ERR(afe->apll2))
		return dev_err_probe(dev, PTR_ERR(afe->apll2),
				     "missing apmixed_apll2_clk\n");

	afe->pinctrl = devm_pinctrl_get(dev);
	if (IS_ERR(afe->pinctrl))
		return dev_err_probe(dev, PTR_ERR(afe->pinctrl),
				     "missing audio pinctrl\n");
	for (i = 0; i < MT8163_PIN_NUM; i++) {
		afe->pins[i] = pinctrl_lookup_state(afe->pinctrl,
						    mt8163_pin_names[i]);
		if (IS_ERR(afe->pins[i]))
			return dev_err_probe(dev, PTR_ERR(afe->pins[i]),
					     "missing pinctrl state %s\n",
					     mt8163_pin_names[i]);
	}

	afe->irq = platform_get_irq(pdev, 0);
	if (afe->irq < 0)
		return afe->irq;
	ret = devm_request_irq(dev, afe->irq, mt8163_afe_irq, 0,
			       "mt8163-afe", afe);
	if (ret)
		return ret;

	platform_set_drvdata(pdev, afe);
	pm_runtime_enable(dev);
	ret = mt8163_afe_safe(afe);
	if (ret)
		goto disable_pm;

	ret = devm_snd_soc_register_component(dev, &mt8163_afe_component,
					      &mt8163_afe_dai, 1);
	if (ret)
		goto disable_pm;
	return 0;

disable_pm:
	pm_runtime_disable(dev);
	return ret;
}

static int mt8163_afe_remove(struct platform_device *pdev)
{
	struct mt8163_afe *afe = platform_get_drvdata(pdev);

	mt8163_afe_safe(afe);
	mt8163_afe_clocks_disable(afe);
	pm_runtime_disable(&pdev->dev);
	return 0;
}

static const struct of_device_id mt8163_afe_of_match[] = {
	{ .compatible = "mediatek,mt8163-soc-pcm-dl1" },
	{ }
};
MODULE_DEVICE_TABLE(of, mt8163_afe_of_match);

static struct platform_driver mt8163_afe_driver = {
	.probe = mt8163_afe_probe,
	.remove = mt8163_afe_remove,
	.driver = {
		.name = "mt8163-afe",
		.of_match_table = mt8163_afe_of_match,
	},
};
module_platform_driver(mt8163_afe_driver);

MODULE_DESCRIPTION("MediaTek MT8163 narrow DL1/I2S AFE driver");
MODULE_LICENSE("GPL");
