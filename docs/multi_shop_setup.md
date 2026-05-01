# Multi-Shop Setup Guide

## Overview

This guide explains how to configure a multi-storefront setup with ERPNext and Shopware 6, including the new **Manufacturer/Brand Filter** feature.

## Features

- **Multiple Sales Channels**: Assign products to different storefronts
- **Item Group Mapping**: Control product visibility based on categories
- **Manufacturer Filter**: Filter products by brand per channel (NEW)
- **Include Subcategories**: Automatically include child categories
- **Visibility Levels**: Control how products appear (All, Search & List, Link Only)

## Setup Steps

### 1. Create Sales Channels in Shopware Admin

For each new shop in Shopware Admin:
1. **Settings > Sales Channels > Create Sales Channel**
2. Select Type: "Storefront"
3. Configure name and domain
4. Assign navigation category

### 2. Sync Sales Channels to ERPNext

1. Open **ERPNext > Shopware Setting**
2. Click **"Refresh Sales Channels"** button
3. All channels will be loaded automatically

### 3. Configure Channel Mappings

In the **"Category Channel Mappings"** table, create entries:

```
Item Group: [Your Category]
Sales Channel: [Your Channel]
Include Subcategories: ✓ (optional)
Manufacturer Filter: [Brand Name] (optional)
Filter Mode: Include Only / Exclude
```

### 4. Set Default Channel

Mark one Sales Channel as **"Is Default"** - products without specific mappings will use this channel.

## Manufacturer/Brand Filter

The new **Manufacturer Filter** field allows filtering products by brand:

### How it works

1. Product's brand is read from the `brand` or `default_item_manufacturer` field
2. If a mapping has a Manufacturer Filter set, only products matching that brand are included

### Filter Modes

- **Include Only**: Only products with this brand are assigned to the channel
- **Exclude**: All products EXCEPT this brand are assigned

### Example Use Cases

**Brand-specific shop:**
```
Item Group: Transport Equipment
Sales Channel: brand-shop.example.com
Manufacturer Filter: BrandX
Filter Mode: Include Only
```
→ Only BrandX products from Transport Equipment category

**Exclude competitor:**
```
Item Group: All Products
Sales Channel: main-shop.example.com
Manufacturer Filter: CompetitorY
Filter Mode: Exclude
```
→ All products except CompetitorY brand

## Visibility Priorities

Products are assigned to channels in this priority order:

1. **Item Override**: Per-item channel assignment (highest priority)
2. **Item Group Mapping + Brand Filter**: Category + brand combination
3. **Item Group Mapping**: Category only
4. **Default Channel**: Fallback for unmapped products

## After Setup

After configuring mappings, sync products:

1. Run **Full Reconciliation** in Shopware Setting
2. Or sync individual products via Item form

Product visibilities will be calculated based on:
- Item Group hierarchy
- Manufacturer Filter (if configured)

## Technical Details

### ERPNext Fields Used

- `item_group`: Product category
- `brand`: Product brand/manufacturer
- `default_item_manufacturer`: Alternative brand field

### Shopware Mapping

- `visibilities`: Product visibility per sales channel
- `manufacturerId`: Linked manufacturer in Shopware
