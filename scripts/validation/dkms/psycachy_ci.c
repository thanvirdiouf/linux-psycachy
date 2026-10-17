// SPDX-License-Identifier: GPL-2.0-only
#include <linux/init.h>
#include <linux/module.h>

static int __init psycachy_ci_init(void)
{
	pr_info("PsyCachy CI external module loaded\n");
	return 0;
}

static void __exit psycachy_ci_exit(void)
{
	pr_info("PsyCachy CI external module unloaded\n");
}

module_init(psycachy_ci_init);
module_exit(psycachy_ci_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PsyCachy package install and boot validation fixture");
