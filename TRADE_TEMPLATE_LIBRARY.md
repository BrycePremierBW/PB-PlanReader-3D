# TradeReader 3D — Trade template library

TradeReader uses one common estimator workflow with a separate starter template for each trade.

Each template contains:

- document checklist;
- standard take-off rows;
- preferred unit for each row;
- measurement basis;
- common exclusions;
- common RFIs / clarification checks;
- zero quantities and zero rates by default.

The template is a starting point only. Project drawings, specifications, schedules, addenda and scope documents remain the authority.

## Included trade templates

### Electrical
Starter lines for switchboards/distribution, GPOs, dedicated connections, lighting, controls, emergency/exit lighting, data/communications, containment, external electrical and testing/commissioning.

### Plumbing
Starter lines for sanitary fixtures, taps/mixers, cold water, hot water, sanitary drainage, stormwater, drains/pits, pumps/plant and testing/commissioning.

### HVAC / Mechanical
Starter lines for mechanical plant, fans, air outlets, ductwork, mechanical pipework, insulation, controls, external mechanical work and testing/balancing/commissioning.

### Carpentry / Joinery
Starter lines for structural carpentry, wall framing, roof framing, doors, frames, skirtings, architraves, joinery, hardware and external timber.

### Plastering / Linings
Starter lines for wall linings, ceilings, bulkheads, wet-area linings, external linings, cornices, trims, access panels and fire/acoustic systems.

### Tiling
Starter lines for floor tiling, wall tiling, skirtings, splashbacks, external tiling, floor/wall waterproofing, movement joints and trims.

### Flooring
Starter lines for carpet, underlay, vinyl/resilient flooring, timber/laminate, floor preparation, skirtings, transitions, stairs and external flooring.

### Roofing
Starter lines for roof sheeting, flashings, gutters, downpipes, roof penetrations, insulation/sarking, roof safety and rainwater goods.

### Concreting
Starter lines for footings, slabs, walls, columns, beams, stairs, pavements, reinforcement, formwork and joints/sundries.

### Landscaping
Starter lines for soil/growing media, paving, retaining, trees, shrubs/groundcovers, mulch, turf, irrigation, fencing/screens and external furniture.

### Custom trade
A neutral starter for primary scope, secondary scope, equipment/fixtures, measured materials, external works, testing/completion and provisional items.

## Rules shared by every template

1. Quantity starts at zero until measured or supported by the documents.
2. Rate starts at zero until entered by the estimator or supplied by an approved source.
3. Keep m², m³, lm, counts, points, sets, kg, tonnes, litres and allowances separate.
4. Never invent routes, lengths, dimensions or hidden quantities.
5. Keep a source reference for measured work.
6. Separate inclusions, exclusions, provisional items, assumptions and RFIs.
7. Confirm trade interfaces rather than silently assigning another trade's work.

The code-ready templates are stored in `tradereader_trade_templates.py` so the TradeReader UI can load them directly when the template-loader step is added.