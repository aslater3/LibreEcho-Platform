// SPDX-License-Identifier: GPL-2.0-only
/*
 * Radar-Puffin codec MCLK.
 *
 * The board routes the MT8163 SENINF test clock to the audio MCLK pin.  A
 * 48 MHz parent divided by five produces the 9.6 MHz clock used by the stock
 * TLV320 configuration.
 */

#include <linux/clk.h>
#include <linux/io.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/pm_domain.h>
#include <linux/pm_runtime.h>

#include "mt8163-mclk.h"

#define MT8163_MCLK_RATE		9600000U

#define SENINF_TOP_CTRL			0x0000
#define SENINF1_CTRL			0x0100
#define SENINF1_MUX_CTRL		0x0120
#define SENINF_TG1_PH_CNT		0x0200
#define SENINF_TG1_SEN_CK		0x0204

#define SENINF1_ENABLE			BIT(0)
#define SENINF1_MUX_ENABLE		BIT(31)
#define SENINF12_PCLK_MASK		GENMASK(11, 10)
#define SENINF12_PCLK_VALUE		GENMASK(9, 8)
#define SENINF_CLK_FALL_MASK		GENMASK(5, 0)
#define SENINF_CLK_RISE_MASK		GENMASK(13, 8)
#define SENINF_CLK_COUNT_MASK		GENMASK(21, 16)
#define SENINF_TGCLK_SEL_MASK		GENMASK(1, 0)
#define SENINF_FALL_POLARITY		BIT(2)
#define SENINF_PADCLK_INVERT		BIT(6)
#define SENINF_CLK_POLARITY		BIT(28)
#define SENINF_PHASE_COUNTER_ENABLE	BIT(31)
#define SENINF_OUTPUT_ENABLE		BIT(29)

struct mt8163_mclk {
	struct device *dev;
	struct platform_device *isp;
	struct platform_device *camera;
	void __iomem *base;
	struct clk *sen_tg;
	struct clk *sen_cam;
	struct clk *camtg;
	struct clk *parent_48m;
	struct clk *saved_parent;
	struct mutex lock;
	unsigned int users;
	bool isp_powered;
	bool sen_tg_enabled;
	bool sen_cam_enabled;
	bool parent_changed;
	bool camtg_enabled;
	bool isp_domain_attached;
	bool isp_pm_enabled;
};

static struct platform_device *mt8163_find_pdev(const char *compatible)
{
	struct platform_device *pdev;
	struct device_node *np;

	np = of_find_compatible_node(NULL, NULL, compatible);
	if (!np)
		return NULL;
	pdev = of_find_device_by_node(np);
	of_node_put(np);
	return pdev;
}

static void mt8163_mclk_update_bits(struct mt8163_mclk *mclk,
				    u32 reg, u32 mask, u32 val)
{
	u32 tmp = readl(mclk->base + reg);

	writel((tmp & ~mask) | (val & mask), mclk->base + reg);
}

static void mt8163_mclk_program(struct mt8163_mclk *mclk)
{
	/* 48 MHz / (4 + 1), with a symmetric-enough 2/3 duty cycle. */
	mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
				SENINF_PHASE_COUNTER_ENABLE,
				SENINF_PHASE_COUNTER_ENABLE);
	mt8163_mclk_update_bits(mclk, SENINF_TOP_CTRL,
				SENINF12_PCLK_MASK, SENINF12_PCLK_VALUE);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_SEN_CK,
				SENINF_CLK_FALL_MASK, 2);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_SEN_CK,
				SENINF_CLK_RISE_MASK, 0);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_SEN_CK,
				SENINF_CLK_COUNT_MASK, 4 << 16);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
				SENINF_TGCLK_SEL_MASK, 1);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
				SENINF_FALL_POLARITY, SENINF_FALL_POLARITY);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
				SENINF_PADCLK_INVERT | SENINF_CLK_POLARITY, 0);
	mt8163_mclk_update_bits(mclk, SENINF1_MUX_CTRL,
				SENINF1_MUX_ENABLE, SENINF1_MUX_ENABLE);
	mt8163_mclk_update_bits(mclk, SENINF1_CTRL,
				SENINF1_ENABLE, SENINF1_ENABLE);
}

static void mt8163_mclk_disable_locked(struct mt8163_mclk *mclk)
{
	if (mclk->base)
		mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
					SENINF_OUTPUT_ENABLE, 0);
	if (mclk->camtg_enabled) {
		clk_disable_unprepare(mclk->camtg);
		mclk->camtg_enabled = false;
	}
	if (mclk->parent_changed && mclk->saved_parent) {
		if (!clk_set_parent(mclk->camtg, mclk->saved_parent))
			mclk->parent_changed = false;
	}
	if (mclk->sen_cam_enabled) {
		clk_disable_unprepare(mclk->sen_cam);
		mclk->sen_cam_enabled = false;
	}
	if (mclk->sen_tg_enabled) {
		clk_disable_unprepare(mclk->sen_tg);
		mclk->sen_tg_enabled = false;
	}
	if (mclk->isp_powered) {
		pm_runtime_put_sync(&mclk->isp->dev);
		mclk->isp_powered = false;
	}
}

int mt8163_mclk_enable(struct mt8163_mclk *mclk)
{
	int ret;

	if (!mclk)
		return -ENODEV;

	mutex_lock(&mclk->lock);
	if (mclk->users++) {
		mutex_unlock(&mclk->lock);
		return 0;
	}

	ret = pm_runtime_resume_and_get(&mclk->isp->dev);
	if (ret < 0)
		goto fail_users;
	mclk->isp_powered = true;
	ret = clk_prepare_enable(mclk->sen_tg);
	if (ret)
		goto fail;
	mclk->sen_tg_enabled = true;
	ret = clk_prepare_enable(mclk->sen_cam);
	if (ret)
		goto fail;
	mclk->sen_cam_enabled = true;

	mclk->saved_parent = clk_get_parent(mclk->camtg);
	if (!mclk->saved_parent) {
		ret = -ENODEV;
		goto fail;
	}
	if (mclk->saved_parent != mclk->parent_48m) {
		ret = clk_set_parent(mclk->camtg, mclk->parent_48m);
		if (ret)
			goto fail;
		mclk->parent_changed = true;
	}
	ret = clk_prepare_enable(mclk->camtg);
	if (ret)
		goto fail;
	mclk->camtg_enabled = true;

	mt8163_mclk_program(mclk);
	mt8163_mclk_update_bits(mclk, SENINF_TG1_PH_CNT,
				SENINF_OUTPUT_ENABLE, SENINF_OUTPUT_ENABLE);
	mutex_unlock(&mclk->lock);
	return 0;

fail:
	mt8163_mclk_disable_locked(mclk);
fail_users:
	mclk->users = 0;
	mutex_unlock(&mclk->lock);
	return ret;
}
EXPORT_SYMBOL_GPL(mt8163_mclk_enable);

void mt8163_mclk_disable(struct mt8163_mclk *mclk)
{
	if (!mclk)
		return;

	mutex_lock(&mclk->lock);
	if (mclk->users && !--mclk->users)
		mt8163_mclk_disable_locked(mclk);
	mutex_unlock(&mclk->lock);
}
EXPORT_SYMBOL_GPL(mt8163_mclk_disable);

static void mt8163_mclk_release(void *data)
{
	struct mt8163_mclk *mclk = data;

	mutex_lock(&mclk->lock);
	mclk->users = 0;
	mt8163_mclk_disable_locked(mclk);
	mutex_unlock(&mclk->lock);
	if (mclk->isp_pm_enabled) {
		pm_runtime_disable(&mclk->isp->dev);
		mclk->isp_pm_enabled = false;
	}
	if (mclk->isp_domain_attached) {
		dev_pm_domain_detach(&mclk->isp->dev, true);
		mclk->isp_domain_attached = false;
	}
	if (mclk->base)
		iounmap(mclk->base);
	if (mclk->parent_48m)
		clk_put(mclk->parent_48m);
	if (mclk->camtg)
		clk_put(mclk->camtg);
	if (mclk->sen_cam)
		clk_put(mclk->sen_cam);
	if (mclk->sen_tg)
		clk_put(mclk->sen_tg);
	if (mclk->camera)
		put_device(&mclk->camera->dev);
	if (mclk->isp)
		put_device(&mclk->isp->dev);
}

struct mt8163_mclk *mt8163_mclk_get(struct device *dev)
{
	struct mt8163_mclk *mclk;
	int ret;

	mclk = devm_kzalloc(dev, sizeof(*mclk), GFP_KERNEL);
	if (!mclk)
		return ERR_PTR(-ENOMEM);
	mclk->dev = dev;
	mutex_init(&mclk->lock);

	mclk->isp = mt8163_find_pdev("mediatek,mt8163-ispsys");
	if (!mclk->isp)
		return ERR_PTR(-EPROBE_DEFER);
	mclk->camera = mt8163_find_pdev("mediatek,mt8163-camera_hw");
	if (!mclk->camera) {
		put_device(&mclk->isp->dev);
		return ERR_PTR(-EPROBE_DEFER);
	}
	ret = dev_pm_domain_attach(&mclk->isp->dev, true);
	if (ret == -ENOENT) {
		/*
		 * MT8163's ISP domain is powered by the SoC bring-up path on
		 * this board.  Some 6.1 DT variants do not expose a matching
		 * genpd provider to this late-created helper, although the ISP
		 * platform device and its clocks are usable (the 3.18 path has
		 * the same assumption).  Do not make codec registration depend
		 * on an optional PM-domain attachment in that case.
		 */
		dev_warn(dev,
			 "ISP PM-domain unavailable (%d); continuing without attachment\n",
			 ret);
		ret = 0;
	} else if (ret) {
		dev_err(dev, "ISP PM-domain attach failed: %d\n", ret);
		goto fail;
	} else {
		mclk->isp_domain_attached = true;
	}
	pm_runtime_enable(&mclk->isp->dev);
	mclk->isp_pm_enabled = true;

	mclk->base = of_iomap(mclk->camera->dev.of_node, 0);
	if (!mclk->base) {
		ret = -ENOMEM;
		goto fail;
	}
	mclk->sen_tg = clk_get(&mclk->isp->dev, "IMG_SEN_TG");
	if (IS_ERR(mclk->sen_tg)) {
		ret = PTR_ERR(mclk->sen_tg);
		dev_err(dev, "IMG_SEN_TG clock lookup failed: %d\n", ret);
		mclk->sen_tg = NULL;
		goto fail;
	}
	mclk->sen_cam = clk_get(&mclk->isp->dev, "IMG_SEN_CAM");
	if (IS_ERR(mclk->sen_cam)) {
		ret = PTR_ERR(mclk->sen_cam);
		dev_err(dev, "IMG_SEN_CAM clock lookup failed: %d\n", ret);
		mclk->sen_cam = NULL;
		goto fail;
	}
	mclk->camtg = clk_get(&mclk->camera->dev, "TOP_CAMTG_SEL");
	if (IS_ERR(mclk->camtg)) {
		ret = PTR_ERR(mclk->camtg);
		dev_err(dev, "TOP_CAMTG_SEL clock lookup failed: %d\n", ret);
		mclk->camtg = NULL;
		goto fail;
	}
	mclk->parent_48m = clk_get(&mclk->camera->dev, "TOP_UNIVPLL_D26");
	if (IS_ERR(mclk->parent_48m)) {
		ret = PTR_ERR(mclk->parent_48m);
		dev_err(dev, "TOP_UNIVPLL_D26 clock lookup failed: %d\n", ret);
		mclk->parent_48m = NULL;
		goto fail;
	}

	ret = devm_add_action_or_reset(dev, mt8163_mclk_release, mclk);
	if (ret)
		return ERR_PTR(ret);
	dev_dbg(dev, "Radar-Puffin codec MCLK configured for %u Hz\n",
		MT8163_MCLK_RATE);
	return mclk;

fail:
	mt8163_mclk_release(mclk);
	return ERR_PTR(ret);
}
EXPORT_SYMBOL_GPL(mt8163_mclk_get);

MODULE_DESCRIPTION("MediaTek MT8163 Radar-Puffin codec MCLK");
MODULE_LICENSE("GPL");
